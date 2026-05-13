from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_run import BudgetRunManager


class BudgetProfileRequest(BaseModel):
    budget_run_state_path: Path
    output_dir: Path | None = None
    rolling_window: int = 60
    min_segment_days: int = 40
    top_n: int = 5
    generate_charts: bool = True


class BudgetProfileResult(BaseModel):
    budget_run_state_path: Path
    output_dir: Path
    profile_json_path: Path
    profile_md_path: Path
    asset_summary_path: Path
    correlation_matrix_path: Path
    asset_metadata_json_path: Path
    asset_metadata_csv_path: Path
    chart_paths: dict[str, Path] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class BudgetProfileService:
    """预算层资产池画像服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetProfileRequest) -> BudgetProfileResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        output_dir = self._resolve_output_dir(request.output_dir, state=state)
        output_dir.mkdir(parents=True, exist_ok=True)

        panel_path = self._resolve_panel_path(state)
        returns_path = self._resolve_returns_path(state)
        split_manifest_path = self._resolve_split_manifest_path(state, required=False)
        signal_manifest_path = self._resolve_signal_manifest_path(state, required=False)
        panel = self._load_panel(panel_path)
        returns = self._load_returns(returns_path)
        symbols = list(returns.columns)

        metadata, metadata_quality, metadata_warnings = self._enrich_asset_metadata(
            symbols=symbols,
            panel=panel,
            signal_manifest_path=signal_manifest_path,
        )
        asset_summary = self._build_asset_summary(
            returns=returns,
            panel=panel,
            metadata=metadata,
        )
        correlation = returns.corr(min_periods=max(20, min(request.rolling_window, len(returns) // 5)))
        correlation_summary = self._correlation_summary(correlation)
        correlation_reference = self._correlation_reference(correlation_summary)
        equal_weight_profile = self._equal_weight_profile(returns)
        rolling_profile = self._rolling_pool_profile(
            returns=returns,
            rolling_window=request.rolling_window,
        )
        regime_segments = self._regime_segments(
            equal_returns=equal_weight_profile["equal_weight_returns"],
            rolling_profile=rolling_profile,
            min_segment_days=request.min_segment_days,
        )
        signal_strategy_profile = self._signal_strategy_profile(metadata=metadata)
        pool_flags = self._pool_flags(
            asset_summary=asset_summary,
            correlation_summary=correlation_summary,
            metadata_quality=metadata_quality,
            regime_segments=regime_segments,
        )

        asset_summary_path = output_dir / "asset_summary.csv"
        correlation_matrix_path = output_dir / "correlation_matrix.csv"
        metadata_json_path = output_dir / "asset_metadata.json"
        metadata_csv_path = output_dir / "asset_metadata.csv"
        profile_json_path = output_dir / "budget_profile.json"
        profile_md_path = output_dir / "budget_profile.md"
        chart_paths: dict[str, Path] = {}

        asset_summary.to_csv(asset_summary_path, index=False, encoding="utf-8-sig")
        correlation.to_csv(correlation_matrix_path, encoding="utf-8-sig")
        metadata_json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        pd.DataFrame(metadata).to_csv(metadata_csv_path, index=False, encoding="utf-8-sig")

        if request.generate_charts:
            chart_paths = self._write_charts(
                output_dir=output_dir,
                returns=returns,
                equal_wealth=equal_weight_profile["equal_weight_wealth"],
                asset_summary=asset_summary,
                correlation=correlation,
                metadata=metadata,
                top_n=request.top_n,
            )

        summary = {
            "symbol_count": len(symbols),
            "start_date": str(returns.index.min().date()),
            "end_date": str(returns.index.max().date()),
            "date_count": int(len(returns)),
            "metadata_quality": metadata_quality,
            "average_pairwise_correlation": correlation_summary.get("average_pairwise_correlation"),
            "equal_weight_total_return": equal_weight_profile["summary"].get("total_return"),
            "equal_weight_max_drawdown": equal_weight_profile["summary"].get("max_drawdown"),
            "regime_segment_count": len(regime_segments),
            "top_sharpe_assets": asset_summary.sort_values("sharpe_like", ascending=False)["symbol"].head(request.top_n).tolist(),
        }
        profile = {
            "created_at": datetime.now().isoformat(),
            "budget_run_id": state.get("budget_run_id"),
            "inputs": {
                "budget_run_state_path": str(self._relative(state_path)),
                "panel_ohlcv_path": str(self._relative(panel_path)),
                "returns_wide_path": str(self._relative(returns_path)),
                "split_manifest_path": str(self._relative(split_manifest_path)) if split_manifest_path else None,
                "signal_manifest_path": str(self._relative(signal_manifest_path)) if signal_manifest_path else None,
            },
            "summary": summary,
            "metadata_quality": metadata_quality,
            "data_coverage": self._data_coverage(panel=panel, returns=returns),
            "asset_summary_path": str(self._relative(asset_summary_path)),
            "correlation_matrix_path": str(self._relative(correlation_matrix_path)),
            "asset_metadata_json_path": str(self._relative(metadata_json_path)),
            "asset_metadata_csv_path": str(self._relative(metadata_csv_path)),
            "correlation_summary": correlation_summary,
            "correlation_reference": correlation_reference,
            "equal_weight_profile": equal_weight_profile["summary"],
            "rolling_pool_profile": rolling_profile["summary"],
            "regime_segments": regime_segments,
            "signal_strategy_profile": signal_strategy_profile,
            "pool_flags": pool_flags,
            "charts": {key: str(self._relative(value)) for key, value in chart_paths.items()},
            "warnings": metadata_warnings,
            "methodology": {
                "metadata_enrichment": "先尝试 MiniQMT xtdata.get_instrument_detail/get_instrument_type；失败后尝试 AKShare；仍失败时规则兜底。",
                "annualization_days": 252,
                "profile_type": "确定性事实画像，不调用 LLM，不输出策略建议。",
                "correlation_usage": "全样本相关性摘要只用于 BudgetAgent 理解资产池和辅助手写 explicit groups；不作为每日执行时的自动分组信号。",
            },
        }
        profile_json_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        profile_md_path.write_text(self._format_markdown(profile, asset_summary), encoding="utf-8")

        self._update_run_state(
            state_path=state_path,
            state=state,
            profile=profile,
            profile_json_path=profile_json_path,
            profile_md_path=profile_md_path,
            asset_summary_path=asset_summary_path,
            correlation_matrix_path=correlation_matrix_path,
            metadata_json_path=metadata_json_path,
            metadata_csv_path=metadata_csv_path,
            chart_paths=chart_paths,
        )

        return BudgetProfileResult(
            budget_run_state_path=state_path,
            output_dir=output_dir,
            profile_json_path=profile_json_path,
            profile_md_path=profile_md_path,
            asset_summary_path=asset_summary_path,
            correlation_matrix_path=correlation_matrix_path,
            asset_metadata_json_path=metadata_json_path,
            asset_metadata_csv_path=metadata_csv_path,
            chart_paths=chart_paths,
            summary=summary,
            warnings=metadata_warnings,
        )

    def _enrich_asset_metadata(
        self,
        *,
        symbols: list[str],
        panel: pd.DataFrame,
        signal_manifest_path: Path | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        signal_lookup = self._load_signal_lookup(signal_manifest_path)
        miniqmt_lookup, miniqmt_errors = self._fetch_miniqmt_metadata(symbols)
        akshare_symbols = [
            symbol
            for symbol in symbols
            if not self._metadata_is_complete(miniqmt_lookup.get(symbol, {}))
        ]
        akshare_lookup, akshare_errors = self._fetch_akshare_metadata(akshare_symbols) if akshare_symbols else ({}, [])
        rows: list[dict[str, Any]] = []
        warnings = miniqmt_errors + akshare_errors
        for symbol in symbols:
            signal_record = signal_lookup.get(symbol, {})
            miniqmt = miniqmt_lookup.get(symbol, {})
            ak = akshare_lookup.get(symbol, {})
            inferred = self._infer_metadata(symbol=symbol, panel=panel)
            name = self._first_non_empty(
                miniqmt.get("name"),
                ak.get("name"),
                signal_record.get("name"),
                inferred.get("name"),
            )
            asset_type = self._first_non_empty(
                miniqmt.get("asset_type"),
                ak.get("asset_type"),
                signal_record.get("asset_type"),
                inferred.get("asset_type"),
            )
            market = self._first_non_empty(miniqmt.get("market"), ak.get("market"), inferred.get("market"))
            industry = self._first_non_empty(miniqmt.get("industry"), ak.get("industry"), inferred.get("industry"))
            theme = self._first_non_empty(miniqmt.get("theme"), ak.get("theme"), inferred.get("theme"))
            fund_type = self._first_non_empty(miniqmt.get("fund_type"), ak.get("fund_type"), inferred.get("fund_type"))
            source = "rule"
            if miniqmt:
                source = "miniqmt"
            elif ak:
                source = "akshare"
            completeness_fields = [name, asset_type, market]
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "asset_type": asset_type,
                    "market": self._normalize_market(symbol, market),
                    "industry": industry,
                    "theme": theme,
                    "fund_type": fund_type,
                    "metadata_source": source,
                    "metadata_completeness": sum(1 for item in completeness_fields if item) / len(completeness_fields),
                    "signal_run_id": signal_record.get("run_id"),
                    "selected_attempt_id": signal_record.get("selected_attempt_id"),
                    "selected_strategy_path": signal_record.get("selected_strategy_path"),
                    "strategy_spec_path": signal_record.get("strategy_spec_path"),
                    "strategy_meta_path": signal_record.get("strategy_meta_path"),
                    "metrics_path": signal_record.get("selected_metrics_path"),
                }
            )
        source_counts = pd.Series([row["metadata_source"] for row in rows]).value_counts().to_dict()
        completeness = [float(row["metadata_completeness"]) for row in rows]
        quality = {
            "source_counts": source_counts,
            "mean_completeness": sum(completeness) / len(completeness) if completeness else 0.0,
            "complete_count": sum(1 for value in completeness if value >= 1.0),
            "partial_count": sum(1 for value in completeness if 0.0 < value < 1.0),
            "missing_count": sum(1 for value in completeness if value <= 0.0),
        }
        return rows, quality, warnings

    @staticmethod
    def _metadata_is_complete(row: dict[str, Any]) -> bool:
        return bool(row.get("name") and row.get("asset_type") and row.get("market"))

    def _fetch_miniqmt_metadata(self, symbols: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        result: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        try:
            from xtquant import xtdata
        except Exception as exc:  # pragma: no cover - depends on local QMT env
            return result, [f"MiniQMT 元数据接口不可用：{exc}"]
        for symbol in symbols:
            try:
                detail = xtdata.get_instrument_detail(symbol, True) or {}
                inst_type = xtdata.get_instrument_type(symbol) or {}
                row = self._metadata_from_xt_detail(symbol=symbol, detail=detail, inst_type=inst_type)
                if row:
                    result[symbol] = row
            except Exception as exc:  # pragma: no cover - depends on local QMT env
                warnings.append(f"{symbol} MiniQMT 元数据查询失败：{exc}")
        return result, warnings

    def _metadata_from_xt_detail(self, *, symbol: str, detail: dict[str, Any], inst_type: dict[str, Any]) -> dict[str, Any]:
        if not detail and not inst_type:
            return {}
        name = self._first_non_empty(
            detail.get("InstrumentName"),
            detail.get("instrument_name"),
            detail.get("Name"),
            detail.get("name"),
            detail.get("ShortName"),
            detail.get("short_name"),
        )
        market = self._first_non_empty(
            detail.get("ExchangeCode"),
            detail.get("exchange_code"),
            detail.get("Market"),
            detail.get("market"),
            symbol.split(".")[-1] if "." in symbol else None,
        )
        asset_type = None
        for key in ["etf", "fund", "index", "stock"]:
            if bool(inst_type.get(key)):
                asset_type = key
                break
        if not asset_type:
            asset_type = self._infer_asset_type_from_symbol(symbol)
        return {
            "name": name,
            "asset_type": asset_type,
            "market": self._normalize_market(symbol, market),
            "industry": self._first_non_empty(detail.get("Industry"), detail.get("industry")),
            "theme": self._first_non_empty(detail.get("ProductName"), detail.get("product_name")),
            "fund_type": "ETF" if asset_type == "etf" else None,
        }

    def _fetch_akshare_metadata(self, symbols: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        result: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        try:
            import akshare as ak
        except Exception as exc:  # pragma: no cover - optional dependency
            return result, [f"AKShare 元数据接口不可用：{exc}"]

        code_to_symbol = {self._code_part(symbol): symbol for symbol in symbols}
        try:
            etf_df = ak.fund_etf_spot_em()
            self._merge_ak_table(result, etf_df, code_to_symbol, asset_type="etf", fund_type="ETF")
        except Exception as exc:
            warnings.append(f"AKShare fund_etf_spot_em 查询失败：{exc}")
        try:
            fund_df = ak.fund_name_em()
            self._merge_ak_table(result, fund_df, code_to_symbol, asset_type="fund")
        except Exception as exc:
            warnings.append(f"AKShare fund_name_em 查询失败：{exc}")
        try:
            stock_df = ak.stock_info_a_code_name()
            self._merge_ak_table(result, stock_df, code_to_symbol, asset_type="stock")
        except Exception as exc:
            warnings.append(f"AKShare stock_info_a_code_name 查询失败：{exc}")
        try:
            index_df = ak.stock_zh_index_spot_em(symbol="上证系列指数")
            self._merge_ak_table(result, index_df, code_to_symbol, asset_type="index")
            index_df = ak.stock_zh_index_spot_em(symbol="深证系列指数")
            self._merge_ak_table(result, index_df, code_to_symbol, asset_type="index")
            index_df = ak.stock_zh_index_spot_em(symbol="指数成份")
            self._merge_ak_table(result, index_df, code_to_symbol, asset_type="index")
        except Exception as exc:
            warnings.append(f"AKShare stock_zh_index_spot_em 查询失败：{exc}")
        return result, warnings

    def _merge_ak_table(
        self,
        result: dict[str, dict[str, Any]],
        df: pd.DataFrame,
        code_to_symbol: dict[str, str],
        *,
        asset_type: str,
        fund_type: str | None = None,
    ) -> None:
        if df is None or df.empty:
            return
        code_col = self._find_col(df, ["代码", "基金代码", "symbol", "证券代码"])
        name_col = self._find_col(df, ["名称", "基金简称", "基金名称", "简称", "股票简称", "指数名称"])
        type_col = self._find_col(df, ["类型", "基金类型", "基金分类"])
        industry_col = self._find_col(df, ["行业", "所属行业"])
        if not code_col:
            return
        for _, row in df.iterrows():
            code = str(row.get(code_col, "")).strip()
            symbol = code_to_symbol.get(code)
            if not symbol:
                continue
            current = result.setdefault(symbol, {})
            current.setdefault("asset_type", asset_type)
            current.setdefault("market", symbol.split(".")[-1] if "." in symbol else None)
            if name_col and pd.notna(row.get(name_col)):
                current["name"] = str(row.get(name_col)).strip()
            if type_col and pd.notna(row.get(type_col)):
                current["fund_type"] = str(row.get(type_col)).strip()
            elif fund_type:
                current.setdefault("fund_type", fund_type)
            if industry_col and pd.notna(row.get(industry_col)):
                current["industry"] = str(row.get(industry_col)).strip()

    def _build_asset_summary(self, *, returns: pd.DataFrame, panel: pd.DataFrame, metadata: list[dict[str, Any]]) -> pd.DataFrame:
        meta_lookup = {row["symbol"]: row for row in metadata}
        rows: list[dict[str, Any]] = []
        for symbol in returns.columns:
            series = pd.to_numeric(returns[symbol], errors="coerce").dropna()
            if series.empty:
                stats = self._empty_asset_stats(symbol)
            else:
                wealth = (1.0 + series).cumprod()
                drawdown = wealth / wealth.cummax() - 1.0
                total_return = float(wealth.iloc[-1] - 1.0)
                annual_return = (1.0 + total_return) ** (252.0 / max(len(series), 1)) - 1.0 if total_return > -1 else -1.0
                annual_vol = float(series.std(ddof=0) * math.sqrt(252.0))
                sharpe = float(series.mean() / series.std(ddof=0) * math.sqrt(252.0)) if series.std(ddof=0) > 0 else 0.0
                max_dd = float(drawdown.min())
                stats = {
                    "symbol": symbol,
                    "valid_return_days": int(series.count()),
                    "first_return_date": str(series.index.min().date()),
                    "last_return_date": str(series.index.max().date()),
                    "total_return": total_return,
                    "annual_return": annual_return,
                    "annual_volatility": annual_vol,
                    "sharpe_like": sharpe,
                    "max_drawdown": max_dd,
                    "calmar_like": annual_return / abs(max_dd) if max_dd < 0 else None,
                    "positive_day_ratio": float((series > 0).mean()),
                    "best_day_return": float(series.max()),
                    "worst_day_return": float(series.min()),
                }
            meta = meta_lookup.get(symbol, {})
            rows.append({**meta, **stats})
        df = pd.DataFrame(rows)
        return df.sort_values(["sharpe_like", "annual_return"], ascending=False).reset_index(drop=True)

    def _correlation_summary(self, corr: pd.DataFrame) -> dict[str, Any]:
        pairs: list[dict[str, Any]] = []
        symbols = list(corr.columns)
        for i, left in enumerate(symbols):
            for right in symbols[i + 1 :]:
                value = corr.loc[left, right]
                if pd.notna(value):
                    pairs.append({"left": left, "right": right, "correlation": float(value)})
        sorted_pairs = sorted(pairs, key=lambda item: item["correlation"], reverse=True)
        avg = sum(item["correlation"] for item in pairs) / len(pairs) if pairs else None
        return {
            "average_pairwise_correlation": avg,
            "highest_pairs": sorted_pairs[:10],
            "lowest_pairs": sorted_pairs[-10:],
            "high_correlation_pair_count": sum(1 for item in pairs if item["correlation"] >= 0.80),
            "low_correlation_pair_count": sum(1 for item in pairs if item["correlation"] <= 0.30),
        }

    def _correlation_reference(self, correlation_summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "purpose": "全样本相关性摘要仅用于 BudgetAgent 理解资产池结构，并在需要时辅助 LLM 手写 explicit groups。",
            "can_be_used_for": [
                "识别资产池中高度相似的资产对。",
                "辅助 BudgetAgent 判断是否需要手写分组。",
                "为 budget_policy_config.json 中的 explicit groups 提供事实依据。",
                "辅助解释组合集中度和分散度风险。",
            ],
            "must_not_be_used_for": [
                "不得作为每日回测或实盘执行中的自动分组信号。",
                "不得直接把全样本相关矩阵传入执行器做动态分组。",
                "不得替代滚动窗口内可得历史数据。",
                "不得只凭相关性摘要生成交易信号。",
            ],
            "grouping_rule": "预算策略执行层第一版只接受 explicit 手写分组；用户指定分组和 LLM 自主分组都必须落入 budget_policy_config.json 的 groups 字段。",
            "average_pairwise_correlation": correlation_summary.get("average_pairwise_correlation"),
            "high_correlation_pair_count": correlation_summary.get("high_correlation_pair_count"),
            "low_correlation_pair_count": correlation_summary.get("low_correlation_pair_count"),
            "highest_pairs": correlation_summary.get("highest_pairs", [])[:10],
            "lowest_pairs": correlation_summary.get("lowest_pairs", [])[:10],
        }

    def _equal_weight_profile(self, returns: pd.DataFrame) -> dict[str, Any]:
        equal_returns = returns.mean(axis=1, skipna=True).fillna(0.0)
        wealth = (1.0 + equal_returns).cumprod()
        drawdown = wealth / wealth.cummax() - 1.0
        total_return = float(wealth.iloc[-1] - 1.0)
        annual_return = (1.0 + total_return) ** (252.0 / max(len(equal_returns), 1)) - 1.0 if total_return > -1 else -1.0
        annual_vol = float(equal_returns.std(ddof=0) * math.sqrt(252.0))
        sharpe = float(equal_returns.mean() / equal_returns.std(ddof=0) * math.sqrt(252.0)) if equal_returns.std(ddof=0) > 0 else 0.0
        return {
            "equal_weight_returns": equal_returns,
            "equal_weight_wealth": wealth,
            "summary": {
                "total_return": total_return,
                "annual_return": annual_return,
                "annual_volatility": annual_vol,
                "sharpe_like": sharpe,
                "max_drawdown": float(drawdown.min()),
                "start_date": str(equal_returns.index.min().date()),
                "end_date": str(equal_returns.index.max().date()),
            },
        }

    def _rolling_pool_profile(self, *, returns: pd.DataFrame, rolling_window: int) -> dict[str, Any]:
        equal_returns = returns.mean(axis=1, skipna=True).fillna(0.0)
        rolling_vol = equal_returns.rolling(rolling_window, min_periods=max(10, rolling_window // 3)).std() * math.sqrt(252.0)
        avg_corr: list[float | None] = []
        dates: list[pd.Timestamp] = []
        min_periods = max(10, rolling_window // 3)
        for end_idx, date in enumerate(returns.index):
            start_idx = max(0, end_idx - rolling_window + 1)
            window = returns.iloc[start_idx : end_idx + 1]
            if len(window) < min_periods:
                dates.append(date)
                avg_corr.append(None)
                continue
            matrix = window.corr(min_periods=min_periods)
            values = []
            columns = list(matrix.columns)
            for i, left in enumerate(columns):
                for right in columns[i + 1 :]:
                    value = matrix.loc[left, right]
                    if pd.notna(value):
                        values.append(float(value))
            dates.append(date)
            avg_corr.append(sum(values) / len(values) if values else None)
        rolling_avg_corr = pd.Series(avg_corr, index=pd.DatetimeIndex(dates), name="rolling_average_correlation")
        cross_section_dispersion = returns.std(axis=1, skipna=True)
        return {
            "rolling_volatility": rolling_vol,
            "rolling_average_correlation": rolling_avg_corr,
            "cross_section_dispersion": cross_section_dispersion,
            "summary": {
                "rolling_window": rolling_window,
                "latest_rolling_volatility": self._safe_float(rolling_vol.dropna().iloc[-1]) if not rolling_vol.dropna().empty else None,
                "latest_average_correlation": self._safe_float(rolling_avg_corr.dropna().iloc[-1]) if not rolling_avg_corr.dropna().empty else None,
                "mean_cross_section_dispersion": self._safe_float(cross_section_dispersion.mean()),
            },
        }

    def _regime_segments(
        self,
        *,
        equal_returns: pd.Series,
        rolling_profile: dict[str, Any],
        min_segment_days: int,
    ) -> list[dict[str, Any]]:
        wealth = (1.0 + equal_returns).cumprod()
        rolling_corr = rolling_profile["rolling_average_correlation"].reindex(equal_returns.index)
        rolling_vol = rolling_profile["rolling_volatility"].reindex(equal_returns.index)
        dispersion = rolling_profile["cross_section_dispersion"].reindex(equal_returns.index)
        labels = []
        corr_q75 = rolling_corr.quantile(0.75)
        vol_q75 = rolling_vol.quantile(0.75)
        for date in equal_returns.index:
            ret60 = wealth.pct_change(60).loc[date] if date in wealth.index else 0.0
            corr = rolling_corr.loc[date]
            vol = rolling_vol.loc[date]
            disp = dispersion.loc[date]
            if pd.notna(corr) and pd.notna(corr_q75) and corr >= corr_q75 and pd.notna(vol) and pd.notna(vol_q75) and vol >= vol_q75:
                label = "high_corr_high_vol"
            elif pd.notna(ret60) and ret60 >= 0.05:
                label = "pool_uptrend"
            elif pd.notna(ret60) and ret60 <= -0.05:
                label = "pool_downtrend"
            elif pd.notna(disp) and disp >= dispersion.quantile(0.75):
                label = "high_dispersion_range"
            else:
                label = "range"
            labels.append(label)
        raw = self._labels_to_segments(list(equal_returns.index), labels)
        merged = self._merge_short_segments(raw, min_segment_days=min_segment_days)
        result: list[dict[str, Any]] = []
        for segment in merged:
            segment_returns = equal_returns.loc[segment["start"] : segment["end"]]
            segment_wealth = (1.0 + segment_returns).cumprod()
            dd = segment_wealth / segment_wealth.cummax() - 1.0
            result.append(
                {
                    **segment,
                    "trading_days": int(len(segment_returns)),
                    "equal_weight_return": self._safe_float(segment_wealth.iloc[-1] - 1.0) if not segment_wealth.empty else None,
                    "max_drawdown": self._safe_float(dd.min()) if not dd.empty else None,
                    "average_correlation": self._safe_float(rolling_corr.loc[segment["start"] : segment["end"]].mean()),
                    "average_volatility": self._safe_float(rolling_vol.loc[segment["start"] : segment["end"]].mean()),
                    "description": self._segment_description(segment, segment_returns, rolling_corr, rolling_vol),
                }
            )
        return result

    def _signal_strategy_profile(self, metadata: list[dict[str, Any]]) -> dict[str, Any]:
        strategy_types: dict[str, int] = {}
        rows = []
        for item in metadata:
            meta_path = item.get("strategy_meta_path")
            strategy_name = None
            structure = None
            if meta_path:
                try:
                    payload = json.loads(self._resolve_path(meta_path).read_text(encoding="utf-8"))
                    strategy_name = payload.get("strategy_name")
                    structure = payload.get("strategy_structure")
                    label = self._first_non_empty(
                        (structure or {}).get("alpha") if isinstance(structure, dict) else None,
                        strategy_name,
                        "unknown",
                    )
                    strategy_types[label] = strategy_types.get(label, 0) + 1
                except Exception:
                    pass
            rows.append(
                {
                    "symbol": item["symbol"],
                    "strategy_name": strategy_name,
                    "strategy_structure": structure,
                    "strategy_spec_path": item.get("strategy_spec_path"),
                    "metrics_path": item.get("metrics_path"),
                }
            )
        return {
            "strategy_type_counts": strategy_types,
            "assets": rows,
        }

    def _pool_flags(
        self,
        *,
        asset_summary: pd.DataFrame,
        correlation_summary: dict[str, Any],
        metadata_quality: dict[str, Any],
        regime_segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        avg_corr = correlation_summary.get("average_pairwise_correlation") or 0.0
        return {
            "high_average_correlation": avg_corr >= 0.65,
            "large_availability_gap": bool((asset_summary["valid_return_days"] < asset_summary["valid_return_days"].max() * 0.75).any()),
            "high_cross_sectional_dispersion": bool(asset_summary["annual_return"].std(ddof=0) > 0.10),
            "signal_quality_dispersion": bool(asset_summary["sharpe_like"].std(ddof=0) > 0.50),
            "metadata_incomplete": (metadata_quality.get("mean_completeness") or 0.0) < 0.9,
            "has_high_corr_high_vol_stage": any(item.get("label") == "high_corr_high_vol" for item in regime_segments),
        }

    def _write_charts(
        self,
        *,
        output_dir: Path,
        returns: pd.DataFrame,
        equal_wealth: pd.Series,
        asset_summary: pd.DataFrame,
        correlation: pd.DataFrame,
        metadata: list[dict[str, Any]],
        top_n: int,
    ) -> dict[str, Path]:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        chart_paths = {
            "cumulative_returns": output_dir / "cumulative_returns.png",
            "correlation_heatmap": output_dir / "correlation_heatmap.png",
            "return_risk_scatter": output_dir / "return_risk_scatter.png",
            "availability_timeline": output_dir / "availability_timeline.png",
        }
        top_symbols = asset_summary.head(top_n)["symbol"].tolist()
        bottom_symbols = asset_summary.tail(top_n)["symbol"].tolist()
        selected = list(dict.fromkeys(top_symbols + bottom_symbols))
        wealth = (1.0 + returns[selected].fillna(0.0)).cumprod()
        fig, ax = plt.subplots(figsize=(13, 7))
        ax.plot(equal_wealth.index, equal_wealth, label="Equal Weight", color="#111827", linewidth=2.0)
        for symbol in selected:
            ax.plot(wealth.index, wealth[symbol], label=symbol, linewidth=0.9, alpha=0.75)
        ax.set_title("Cumulative Returns")
        ax.grid(True, alpha=0.25)
        ax.legend(ncols=3, fontsize=8)
        fig.tight_layout()
        fig.savefig(chart_paths["cumulative_returns"], dpi=160)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 8))
        im = ax.imshow(correlation.fillna(0.0), cmap="RdBu_r", vmin=-1, vmax=1)
        ax.set_xticks(range(len(correlation.columns)))
        ax.set_xticklabels(correlation.columns, rotation=90, fontsize=7)
        ax.set_yticks(range(len(correlation.index)))
        ax.set_yticklabels(correlation.index, fontsize=7)
        ax.set_title("Correlation Matrix")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        fig.tight_layout()
        fig.savefig(chart_paths["correlation_heatmap"], dpi=160)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 7))
        plot_df = asset_summary.copy()
        size = (plot_df["valid_return_days"] / plot_df["valid_return_days"].max()).fillna(0.2) * 350
        ax.scatter(plot_df["annual_volatility"], plot_df["annual_return"], s=size, alpha=0.70, color="#2563eb")
        for _, row in plot_df.iterrows():
            ax.annotate(row["symbol"], (row["annual_volatility"], row["annual_return"]), fontsize=7, xytext=(3, 3), textcoords="offset points")
        ax.axhline(0, color="#64748b", linewidth=0.8)
        ax.set_xlabel("Annualized Volatility")
        ax.set_ylabel("Annualized Return")
        ax.set_title("Return vs Risk")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(chart_paths["return_risk_scatter"], dpi=160)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(12, 7))
        meta_lookup = {item["symbol"]: item for item in metadata}
        for idx, symbol in enumerate(asset_summary["symbol"].tolist()):
            start = pd.to_datetime(asset_summary.loc[asset_summary["symbol"] == symbol, "first_return_date"].iloc[0])
            end = pd.to_datetime(asset_summary.loc[asset_summary["symbol"] == symbol, "last_return_date"].iloc[0])
            ax.hlines(idx, start, end, color="#2563eb", linewidth=4)
            ax.text(end, idx, f" {symbol}", va="center", fontsize=7)
        ax.set_yticks([])
        ax.set_title("Asset Availability Timeline")
        ax.grid(True, axis="x", alpha=0.25)
        fig.tight_layout()
        fig.savefig(chart_paths["availability_timeline"], dpi=160)
        plt.close(fig)
        return chart_paths

    def _format_markdown(self, profile: dict[str, Any], asset_summary: pd.DataFrame) -> str:
        summary = profile["summary"]
        lines = [
            "# Budget Profile",
            "",
            "## Summary",
            f"- budget_run_id: {profile.get('budget_run_id')}",
            f"- assets: {summary['symbol_count']}",
            f"- date_range: {summary['start_date']} to {summary['end_date']}",
            f"- equal_weight_total_return: {self._pct(summary.get('equal_weight_total_return'))}",
            f"- equal_weight_max_drawdown: {self._pct(summary.get('equal_weight_max_drawdown'))}",
            f"- average_pairwise_correlation: {self._fmt(summary.get('average_pairwise_correlation'))}",
            f"- metadata_mean_completeness: {self._fmt(summary['metadata_quality'].get('mean_completeness'))}",
            "",
            "## Charts",
        ]
        for _, path in profile.get("charts", {}).items():
            lines.append(f"- {path}")
        lines.extend(["", "## Top Assets By Sharpe-Like"])
        for _, row in asset_summary.head(10).iterrows():
            lines.append(
                f"- {row['symbol']} {row.get('name') or ''}: sharpe={self._fmt(row.get('sharpe_like'))}, "
                f"annual_return={self._pct(row.get('annual_return'))}, max_dd={self._pct(row.get('max_drawdown'))}"
            )
        correlation_reference = profile.get("correlation_reference", {})
        lines.extend(
            [
                "",
                "## Correlation Reference",
                f"- purpose: {correlation_reference.get('purpose')}",
                f"- average_pairwise_correlation: {self._fmt(correlation_reference.get('average_pairwise_correlation'))}",
                f"- high_correlation_pair_count: {correlation_reference.get('high_correlation_pair_count')}",
                f"- low_correlation_pair_count: {correlation_reference.get('low_correlation_pair_count')}",
                f"- grouping_rule: {correlation_reference.get('grouping_rule')}",
                "- note: 全样本相关性摘要只用于理解资产池和辅助手写 explicit groups，不作为每日执行时的自动分组信号。",
                "",
                "### Highest Correlation Pairs",
            ]
        )
        for item in correlation_reference.get("highest_pairs", [])[:10]:
            lines.append(f"- {item['left']} / {item['right']}: {self._fmt(item.get('correlation'))}")
        lines.extend(["", "## Pool Flags"])
        for key, value in profile.get("pool_flags", {}).items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## Regime Segments"])
        for item in profile.get("regime_segments", []):
            lines.append(f"- {item['description']}")
        lines.extend(
            [
                "",
                "## Notes",
                "- 本画像只描述事实和统计结果，不提供预算策略建议。",
                "- 全样本相关性摘要只用于理解资产池结构和辅助手写 explicit groups，不作为每日执行时的自动分组信号。",
                "",
            ]
        )
        return "\n".join(lines)

    def _update_run_state(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        profile: dict[str, Any],
        profile_json_path: Path,
        profile_md_path: Path,
        asset_summary_path: Path,
        correlation_matrix_path: Path,
        metadata_json_path: Path,
        metadata_csv_path: Path,
        chart_paths: dict[str, Path],
    ) -> None:
        now = datetime.now().isoformat()
        state.setdefault("asset_pool", {})["asset_metadata_path"] = str(self._relative(metadata_json_path))
        state.setdefault("budget_profile", {}).update(
            {
                "status": "success",
                "profile_json": str(self._relative(profile_json_path)),
                "profile_md": str(self._relative(profile_md_path)),
                "charts": [str(self._relative(path)) for path in chart_paths.values()],
                "summary": profile["summary"],
                "error": None,
            }
        )
        state.setdefault("artifacts", {}).setdefault("profile", {})["budget_profile"] = {
            "profile_json": str(self._relative(profile_json_path)),
            "profile_md": str(self._relative(profile_md_path)),
            "asset_summary": str(self._relative(asset_summary_path)),
            "correlation_matrix": str(self._relative(correlation_matrix_path)),
            "asset_metadata_json": str(self._relative(metadata_json_path)),
            "asset_metadata_csv": str(self._relative(metadata_csv_path)),
            "charts": {key: str(self._relative(path)) for key, path in chart_paths.items()},
            "summary": profile["summary"],
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetProfileService",
                "event": "budget_profile_completed",
                "summary": f"预算层资产池画像已生成，资产数：{profile['summary']['symbol_count']}。",
                "profile_json": str(self._relative(profile_json_path)),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _data_coverage(self, *, panel: pd.DataFrame, returns: pd.DataFrame) -> dict[str, Any]:
        available_counts = returns.notna().sum(axis=1)
        return {
            "panel_row_count": int(len(panel)),
            "returns_date_count": int(len(returns)),
            "symbol_count": int(len(returns.columns)),
            "min_available_assets_per_day": int(available_counts.min()),
            "max_available_assets_per_day": int(available_counts.max()),
            "mean_available_assets_per_day": float(available_counts.mean()),
        }

    def _load_signal_lookup(self, signal_manifest_path: Path | None) -> dict[str, dict[str, Any]]:
        if not signal_manifest_path or not signal_manifest_path.exists():
            return {}
        payload = json.loads(signal_manifest_path.read_text(encoding="utf-8"))
        return {str(item.get("symbol", "")).upper(): item for item in payload.get("records", [])}

    def _infer_metadata(self, *, symbol: str, panel: pd.DataFrame) -> dict[str, Any]:
        return {
            "name": symbol,
            "asset_type": self._infer_asset_type_from_symbol(symbol),
            "market": symbol.split(".")[-1] if "." in symbol else None,
            "industry": None,
            "theme": None,
            "fund_type": "ETF" if self._infer_asset_type_from_symbol(symbol) == "etf" else None,
        }

    def _infer_asset_type_from_symbol(self, symbol: str) -> str:
        code = self._code_part(symbol)
        if code.startswith(("51", "15", "56", "58")):
            return "etf"
        if code.startswith(("000", "399")):
            return "index"
        return "stock"

    @staticmethod
    def _normalize_market(symbol: str, market: Any) -> str | None:
        suffix = symbol.split(".")[-1].upper() if "." in symbol else None
        if suffix in {"SH", "SZ"}:
            return suffix
        if market is None:
            return suffix
        text = str(market).strip().upper()
        if text in {"SH", "SSE", "上海", "上交所"}:
            return "SH"
        if text in {"SZ", "SZSE", "深圳", "深交所"}:
            return "SZ"
        return suffix or text or None

    def _labels_to_segments(self, dates: list[pd.Timestamp], labels: list[str]) -> list[dict[str, Any]]:
        if not dates:
            return []
        segments = []
        start_idx = 0
        current = labels[0]
        for idx, label in enumerate(labels[1:], start=1):
            if label != current:
                segments.append({"start": dates[start_idx], "end": dates[idx - 1], "label": current})
                start_idx = idx
                current = label
        segments.append({"start": dates[start_idx], "end": dates[-1], "label": current})
        return segments

    def _merge_short_segments(self, segments: list[dict[str, Any]], min_segment_days: int) -> list[dict[str, Any]]:
        if not segments:
            return []
        merged: list[dict[str, Any]] = []
        for segment in segments:
            days = (segment["end"] - segment["start"]).days + 1
            if merged and days < min_segment_days:
                merged[-1]["end"] = segment["end"]
            else:
                merged.append(segment.copy())
        return merged

    def _segment_description(self, segment: dict[str, Any], segment_returns: pd.Series, rolling_corr: pd.Series, rolling_vol: pd.Series) -> str:
        start = str(pd.to_datetime(segment["start"]).date())
        end = str(pd.to_datetime(segment["end"]).date())
        total_return = (1.0 + segment_returns).prod() - 1.0 if not segment_returns.empty else None
        return (
            f"{start} 至 {end}，阶段标签 {segment['label']}，交易日 {len(segment_returns)}，"
            f"等权收益 {self._pct(total_return)}，平均相关性 {self._fmt(rolling_corr.loc[segment['start']:segment['end']].mean())}，"
            f"平均年化波动 {self._pct(rolling_vol.loc[segment['start']:segment['end']].mean())}。"
        )

    @staticmethod
    def _empty_asset_stats(symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "valid_return_days": 0,
            "first_return_date": None,
            "last_return_date": None,
            "total_return": None,
            "annual_return": None,
            "annual_volatility": None,
            "sharpe_like": None,
            "max_drawdown": None,
            "calmar_like": None,
            "positive_day_ratio": None,
            "best_day_return": None,
            "worst_day_return": None,
        }

    def _load_panel(self, panel_path: Path) -> pd.DataFrame:
        df = pd.read_parquet(panel_path)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce").dt.normalize()
        return df.dropna(subset=["datetime", "symbol"]).sort_values(["datetime", "symbol"]).reset_index(drop=True)

    def _load_returns(self, returns_path: Path) -> pd.DataFrame:
        df = pd.read_parquet(returns_path)
        df.index = pd.to_datetime(df.index, errors="coerce").normalize()
        df = df.loc[df.index.notna()].sort_index()
        return df

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        profile_dir = state.get("directories", {}).get("profile")
        if not profile_dir:
            raise ValueError("budget_run_state.json 缺少 directories.profile。")
        return self._resolve_path(profile_dir)

    def _resolve_panel_path(self, state: dict[str, Any]) -> Path:
        candidate = state.get("data_panel", {}).get("panel_ohlcv")
        if not candidate:
            raise ValueError("budget_run_state.json 缺少 data_panel.panel_ohlcv，请先运行 budget-data-panel。")
        return self._resolve_path(candidate)

    def _resolve_returns_path(self, state: dict[str, Any]) -> Path:
        candidate = state.get("data_panel", {}).get("returns_wide")
        if not candidate:
            raise ValueError("budget_run_state.json 缺少 data_panel.returns_wide，请先运行 budget-data-panel。")
        return self._resolve_path(candidate)

    def _resolve_split_manifest_path(self, state: dict[str, Any], *, required: bool) -> Path | None:
        candidate = state.get("data_split", {}).get("split_manifest")
        if not candidate and required:
            raise ValueError("budget_run_state.json 缺少 data_split.split_manifest，请先运行 budget-data-split。")
        return self._resolve_path(candidate) if candidate else None

    def _resolve_signal_manifest_path(self, state: dict[str, Any], *, required: bool) -> Path | None:
        candidate = state.get("signal_artifacts", {}).get("manifest_path")
        if not candidate and required:
            raise ValueError("budget_run_state.json 缺少 signal_artifacts.manifest_path。")
        return self._resolve_path(candidate) if candidate else None

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: str | Path | None) -> str | None:
        if path is None:
            return None
        value = Path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)

    @staticmethod
    def _code_part(symbol: str) -> str:
        return symbol.split(".")[0]

    @staticmethod
    def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
        lookup = {str(col).lower(): col for col in df.columns}
        for candidate in candidates:
            if candidate in df.columns:
                return candidate
            value = lookup.get(candidate.lower())
            if value is not None:
                return str(value)
        return None

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value is None:
                continue
            if isinstance(value, float) and math.isnan(value):
                continue
            text = str(value).strip()
            if text:
                return value
        return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    def _pct(self, value: Any) -> str:
        number = self._safe_float(value)
        if number is None:
            return "未知"
        return f"{number:.2%}"

    def _fmt(self, value: Any) -> str:
        number = self._safe_float(value)
        if number is None:
            return "未知"
        return f"{number:.4f}"
