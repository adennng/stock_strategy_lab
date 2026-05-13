from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_policy_engine import BudgetPolicyEngine, BudgetPolicyEngineRequest
from strategy_lab.services.data_format import load_wide_parquet, normalize_symbol_series
from strategy_lab.services.portfolio_run import PortfolioRunManager


class PortfolioProfileRequest(BaseModel):
    portfolio_run_state_path: Path
    split_manifest_path: Path | None = None
    output_dir: Path | None = None
    generate_charts: bool = True
    update_run_state: bool = True


class PortfolioProfileResult(BaseModel):
    portfolio_run_state_path: Path
    output_dir: Path
    profile_json_path: Path
    profile_md_path: Path
    asset_summary_path: Path
    budget_summary_path: Path
    signal_summary_path: Path
    budget_signal_alignment_path: Path
    daily_budget_signal_alignment_path: Path
    correlation_matrix_path: Path
    budget_benchmark_metrics_json_path: Path
    budget_benchmark_metrics_csv_path: Path
    budget_benchmark_equity_path: Path
    daily_budget_weights_path: Path
    daily_signal_targets_path: Path
    chart_paths: dict[str, Path] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class PortfolioProfileService:
    """组合层画像服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = PortfolioRunManager(config=self.config)

    def run(self, request: PortfolioProfileRequest) -> PortfolioProfileResult:
        state_path = self._resolve_path(request.portfolio_run_state_path)
        state = self.run_manager.load_state(state_path)
        split_manifest_path = self._resolve_split_manifest_path(request.split_manifest_path, state=state)
        split_manifest = self._read_json(split_manifest_path)
        panel_path, returns_path = self._resolve_data_paths(split_manifest)
        output_dir = self._resolve_output_dir(request.output_dir, state=state)
        output_dir.mkdir(parents=True, exist_ok=True)
        charts_dir = output_dir / "charts"
        charts_dir.mkdir(parents=True, exist_ok=True)

        reference_symbols = self._reference_symbols_from_state(state)
        panel = self._load_panel(panel_path, reference_symbols=reference_symbols)
        returns = self._load_wide_frame(returns_path, reference_symbols=reference_symbols)

        budget_weights, budget_warnings = self._build_budget_weights(
            state=state,
            panel_path=panel_path,
            returns_path=returns_path,
            output_dir=output_dir,
            reference_symbols=reference_symbols,
        )
        signal_targets, signal_warnings = self._build_signal_targets(
            state=state,
            panel=panel,
            target_index=budget_weights.index.intersection(returns.index),
            target_columns=list(budget_weights.columns),
        )

        index = budget_weights.index.intersection(signal_targets.index).intersection(returns.index)
        columns = sorted(set(budget_weights.columns).intersection(signal_targets.columns).intersection(returns.columns))
        if len(index) == 0 or not columns:
            raise ValueError("预算权重、信号目标仓位和收益率没有可对齐的日期或资产。")
        budget_weights = budget_weights.loc[index, columns].fillna(0.0)
        signal_targets = signal_targets.loc[index, columns].fillna(0.0)
        returns = returns.loc[index, columns].fillna(0.0)

        daily_budget_weights_path = output_dir / "daily_budget_weights.parquet"
        daily_signal_targets_path = output_dir / "daily_signal_targets.parquet"
        budget_weights.to_parquet(daily_budget_weights_path)
        signal_targets.to_parquet(daily_signal_targets_path)

        asset_summary = self._build_asset_summary(panel=panel, returns=returns, symbols=columns)
        budget_summary = self._build_budget_summary(budget_weights)
        signal_summary = self._build_signal_summary(signal_targets)
        alignment = self._build_alignment_summary(
            budget_weights=budget_weights,
            signal_targets=signal_targets,
        )
        daily_alignment = self._build_daily_alignment_summary(budget_weights=budget_weights, signal_targets=signal_targets)
        benchmark_payload, benchmark_metrics, benchmark_equity = self._build_budget_benchmark_profile(
            budget_weights=budget_weights,
            returns=returns,
        )
        correlation = returns.corr().fillna(0.0)
        profile = self._build_profile_payload(
            state=state,
            split_manifest_path=split_manifest_path,
            panel_path=panel_path,
            returns_path=returns_path,
            asset_summary=asset_summary,
            budget_summary=budget_summary,
            signal_summary=signal_summary,
            alignment=alignment,
            daily_alignment=daily_alignment,
            correlation=correlation,
            budget_weights=budget_weights,
            signal_targets=signal_targets,
            benchmark_payload=benchmark_payload,
            warnings=budget_warnings + signal_warnings,
        )

        asset_summary_path = output_dir / "asset_summary.csv"
        budget_summary_path = output_dir / "budget_summary.csv"
        signal_summary_path = output_dir / "signal_summary.csv"
        alignment_path = output_dir / "budget_signal_alignment.csv"
        daily_alignment_path = output_dir / "daily_budget_signal_alignment.parquet"
        correlation_path = output_dir / "correlation_matrix.csv"
        benchmark_metrics_json_path = output_dir / "budget_benchmark_metrics.json"
        benchmark_metrics_csv_path = output_dir / "budget_benchmark_metrics.csv"
        benchmark_equity_path = output_dir / "budget_benchmark_equity.parquet"
        profile_json_path = output_dir / "portfolio_profile.json"
        profile_md_path = output_dir / "portfolio_profile.md"
        asset_summary.to_csv(asset_summary_path, index=False, encoding="utf-8-sig")
        budget_summary.to_csv(budget_summary_path, index=False, encoding="utf-8-sig")
        signal_summary.to_csv(signal_summary_path, index=False, encoding="utf-8-sig")
        alignment.to_csv(alignment_path, index=False, encoding="utf-8-sig")
        daily_alignment.to_parquet(daily_alignment_path)
        correlation.to_csv(correlation_path, encoding="utf-8-sig")
        benchmark_metrics_json_path.write_text(json.dumps(benchmark_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        benchmark_metrics.to_csv(benchmark_metrics_csv_path, index=False, encoding="utf-8-sig")
        benchmark_equity.to_parquet(benchmark_equity_path)
        profile_json_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        profile_md_path.write_text(self._format_markdown(profile, alignment=alignment), encoding="utf-8")

        chart_paths: dict[str, Path] = {}
        if request.generate_charts:
            chart_paths = self._write_charts(
                output_dir=charts_dir,
                alignment=alignment,
                daily_alignment=daily_alignment,
                correlation=correlation,
                budget_weights=budget_weights,
                signal_targets=signal_targets,
                benchmark_equity=benchmark_equity,
            )

        warnings = budget_warnings + signal_warnings
        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                profile=profile,
                output_dir=output_dir,
                profile_json_path=profile_json_path,
                profile_md_path=profile_md_path,
                alignment_path=alignment_path,
                daily_alignment_path=daily_alignment_path,
                benchmark_metrics_json_path=benchmark_metrics_json_path,
                benchmark_metrics_csv_path=benchmark_metrics_csv_path,
                benchmark_equity_path=benchmark_equity_path,
                daily_budget_weights_path=daily_budget_weights_path,
                daily_signal_targets_path=daily_signal_targets_path,
                chart_paths=chart_paths,
            )

        return PortfolioProfileResult(
            portfolio_run_state_path=state_path,
            output_dir=output_dir,
            profile_json_path=profile_json_path,
            profile_md_path=profile_md_path,
            asset_summary_path=asset_summary_path,
            budget_summary_path=budget_summary_path,
            signal_summary_path=signal_summary_path,
            budget_signal_alignment_path=alignment_path,
            daily_budget_signal_alignment_path=daily_alignment_path,
            correlation_matrix_path=correlation_path,
            budget_benchmark_metrics_json_path=benchmark_metrics_json_path,
            budget_benchmark_metrics_csv_path=benchmark_metrics_csv_path,
            budget_benchmark_equity_path=benchmark_equity_path,
            daily_budget_weights_path=daily_budget_weights_path,
            daily_signal_targets_path=daily_signal_targets_path,
            chart_paths=chart_paths,
            warnings=warnings,
        )

    def _build_budget_weights(
        self,
        *,
        state: dict[str, Any],
        panel_path: Path,
        returns_path: Path,
        output_dir: Path,
        reference_symbols: list[str],
    ) -> tuple[pd.DataFrame, list[str]]:
        budget = state.get("source_artifacts", {}).get("budget", {}) or {}
        budget_state_path = self._resolve_path(budget.get("budget_run_state_path") or budget.get("source_budget_run_state_path"))
        policy_config_path = self._resolve_path(budget.get("final_budget_policy_config_path"))
        budget_result = BudgetPolicyEngine(config=self.config).run(
            BudgetPolicyEngineRequest(
                budget_run_state_path=budget_state_path,
                policy_config_path=policy_config_path,
                output_dir=output_dir / "budget_execution",
                panel_ohlcv_path=panel_path,
                returns_wide_path=returns_path,
                policy_id="portfolio_profile_budget",
                update_run_state=False,
            )
        )
        weights = self._load_wide_frame(budget_result.daily_budget_weights_path, reference_symbols=reference_symbols)
        return weights, list(budget_result.warnings)

    def _build_signal_targets(
        self,
        *,
        state: dict[str, Any],
        panel: pd.DataFrame,
        target_index: pd.DatetimeIndex,
        target_columns: list[str],
    ) -> tuple[pd.DataFrame, list[str]]:
        warnings: list[str] = []
        targets = pd.DataFrame(0.0, index=target_index, columns=target_columns, dtype=float)
        panel_by_symbol = {symbol: frame.sort_values("datetime").reset_index(drop=True) for symbol, frame in panel.groupby("symbol")}
        for item in state.get("source_artifacts", {}).get("signals", {}).get("items", []):
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in targets.columns:
                warnings.append(f"{symbol} 不在预算权重列中，跳过信号画像。")
                continue
            symbol_panel = panel_by_symbol.get(symbol)
            if symbol_panel is None or symbol_panel.empty:
                warnings.append(f"{symbol} 没有可用行情，信号目标仓位置为 0。")
                continue
            copied = item.get("copied_files") if isinstance(item.get("copied_files"), dict) else {}
            strategy_path = self._resolve_path(copied.get("selected_strategy_path") or item.get("selected_strategy_path") or copied.get("strategy_path"))
            strategy_meta_path = self._resolve_optional_path(copied.get("strategy_meta_path"), default=strategy_path.parent / "strategy_meta.json")
            param_space_path = self._resolve_optional_path(copied.get("param_space_path"), default=strategy_path.parent / "param_space.json")
            run_state_path = self._resolve_optional_path(copied.get("run_state_path"), default=strategy_path.parent / "run_state.json")
            class_name = self._resolve_strategy_class_name(strategy_meta_path)
            strategy_cls = self._load_strategy_class(strategy_path, class_name)
            params = self._resolve_signal_params(run_state_path=run_state_path, param_space_path=param_space_path)
            strategy = strategy_cls(params)
            symbol_targets = self._run_signal_strategy(strategy=strategy, symbol_panel=symbol_panel)
            targets[symbol] = symbol_targets.reindex(targets.index).ffill().fillna(0.0).clip(lower=0.0, upper=1.0)
        return targets.fillna(0.0), warnings

    def _run_signal_strategy(self, *, strategy: Any, symbol_panel: pd.DataFrame) -> pd.Series:
        values: list[dict[str, Any]] = []
        previous = 0.0
        for idx, row in symbol_panel.iterrows():
            history = symbol_panel.iloc[: idx + 1].copy()
            try:
                target = float(strategy.suggest(history, current_position_in_budget=previous))
            except TypeError:
                target = float(strategy.suggest(history))
            target = max(0.0, min(1.0, target))
            values.append({"datetime": row["datetime"], "target": target})
            previous = target
        series = pd.DataFrame(values).drop_duplicates("datetime", keep="last").set_index("datetime")["target"]
        series.index = pd.to_datetime(series.index).normalize()
        return series.sort_index()

    def _build_asset_summary(self, *, panel: pd.DataFrame, returns: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            symbol_panel = panel.loc[panel["symbol"] == symbol].sort_values("datetime")
            close = pd.to_numeric(symbol_panel.get("close"), errors="coerce").dropna()
            ret = returns[symbol].dropna() if symbol in returns.columns else pd.Series(dtype=float)
            total_return = None
            max_drawdown = None
            if len(close) >= 2 and close.iloc[0] != 0:
                total_return = float(close.iloc[-1] / close.iloc[0] - 1.0)
                curve = close / close.iloc[0]
                max_drawdown = float((curve / curve.cummax() - 1.0).min())
            rows.append(
                {
                    "symbol": symbol,
                    "start": self._date_text(symbol_panel["datetime"].min()),
                    "end": self._date_text(symbol_panel["datetime"].max()),
                    "row_count": int(len(symbol_panel)),
                    "total_return": total_return,
                    "annual_volatility": float(ret.std() * np.sqrt(252)) if len(ret) > 1 else None,
                    "max_drawdown": max_drawdown,
                    "avg_volume": self._safe_mean(symbol_panel.get("volume")),
                    "missing_return_ratio": float(ret.isna().mean()) if len(ret) else None,
                }
            )
        return pd.DataFrame(rows)

    def _build_budget_summary(self, budget_weights: pd.DataFrame) -> pd.DataFrame:
        ranks = budget_weights.rank(axis=1, ascending=False, method="average")
        return pd.DataFrame(
            {
                "symbol": budget_weights.columns,
                "budget_mean": budget_weights.mean().values,
                "budget_max": budget_weights.max().values,
                "budget_std": budget_weights.std().values,
                "budget_zero_ratio": budget_weights.le(1e-12).mean().values,
                "budget_active_ratio": budget_weights.gt(1e-12).mean().values,
                "budget_rank_mean": ranks.mean().values,
            }
        ).sort_values("budget_mean", ascending=False)

    def _build_signal_summary(self, signal_targets: pd.DataFrame) -> pd.DataFrame:
        changes = signal_targets.diff().abs().fillna(0.0).gt(1e-9).sum()
        return pd.DataFrame(
            {
                "symbol": signal_targets.columns,
                "signal_mean": signal_targets.mean().values,
                "signal_max": signal_targets.max().values,
                "signal_std": signal_targets.std().values,
                "signal_zero_ratio": signal_targets.le(1e-12).mean().values,
                "signal_gt_03_ratio": signal_targets.gt(0.3).mean().values,
                "signal_gt_06_ratio": signal_targets.gt(0.6).mean().values,
                "signal_change_count": changes.values,
            }
        ).sort_values("signal_mean", ascending=False)

    def _build_alignment_summary(
        self,
        *,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
    ) -> pd.DataFrame:
        budget_share = self._row_share(budget_weights)
        signal_share = self._row_share(signal_targets)
        budget_ranks = budget_weights.rank(axis=1, ascending=False, method="average")
        signal_ranks = signal_targets.rank(axis=1, ascending=False, method="average")
        budget_pct_ranks = budget_weights.rank(axis=1, pct=True, method="average")
        signal_pct_ranks = signal_targets.rank(axis=1, pct=True, method="average")
        top_budget_threshold = budget_weights.quantile(0.70, axis=1)
        low_signal_threshold = signal_targets.quantile(0.30, axis=1)
        top_signal_threshold = signal_targets.quantile(0.70, axis=1)
        low_budget_threshold = budget_weights.quantile(0.30, axis=1)
        rows: list[dict[str, Any]] = []
        for symbol in budget_weights.columns:
            b = budget_weights[symbol].fillna(0.0)
            s = signal_targets[symbol].fillna(0.0)
            bs = budget_share[symbol].fillna(0.0)
            ss = signal_share[symbol].fillna(0.0)
            br = budget_ranks[symbol]
            sr = signal_ranks[symbol]
            bpr = budget_pct_ranks[symbol]
            spr = signal_pct_ranks[symbol]
            corr = b.corr(s) if b.std() > 0 and s.std() > 0 else None
            strong_signal_mask = s >= max(float(s.quantile(0.75)), 1e-12)
            budget_top_mask = b >= top_budget_threshold
            budget_high_signal_low_mask = (b >= top_budget_threshold) & (s <= low_signal_threshold)
            signal_high_budget_low_mask = (s >= top_signal_threshold) & (b <= low_budget_threshold)
            rows.append(
                {
                    "symbol": symbol,
                    "budget_mean": float(b.mean()),
                    "signal_mean": float(s.mean()),
                    "budget_share_mean": float(bs.mean()),
                    "signal_share_mean": float(ss.mean()),
                    "share_gap_mean": float((ss - bs).mean()),
                    "budget_rank_mean": float(br.mean()),
                    "signal_rank_mean": float(sr.mean()),
                    "rank_gap_mean": float((sr - br).mean()),
                    "budget_pct_rank_mean": float(bpr.mean()),
                    "signal_pct_rank_mean": float(spr.mean()),
                    "pct_rank_gap_mean": float((spr - bpr).mean()),
                    "budget_signal_corr": None if pd.isna(corr) else float(corr),
                    "budget_share_when_signal_strong": self._safe_mask_mean(bs, strong_signal_mask),
                    "budget_rank_when_signal_strong": self._safe_mask_mean(br, strong_signal_mask),
                    "budget_zero_when_signal_strong_ratio": self._safe_mask_mean(b.le(1e-12), strong_signal_mask),
                    "signal_when_budget_top_mean": self._safe_mask_mean(s, budget_top_mask),
                    "signal_zero_when_budget_top_ratio": self._safe_mask_mean(s.le(1e-12), budget_top_mask),
                    "signal_confirm_when_budget_top_ratio": self._safe_mask_mean(s.ge(0.3), budget_top_mask),
                    "budget_high_signal_low_days_ratio": float(budget_high_signal_low_mask.mean()),
                    "signal_high_budget_low_days_ratio": float(signal_high_budget_low_mask.mean()),
                    "alignment_type": self._classify_alignment(
                        share_gap=float((ss - bs).mean()),
                        rank_gap=float((sr - br).mean()),
                        budget_high_signal_low_ratio=float(budget_high_signal_low_mask.mean()),
                        signal_high_budget_low_ratio=float(signal_high_budget_low_mask.mean()),
                        active_ratio=float(s.gt(1e-12).mean()),
                    ),
                    "high_budget_low_signal": bool(float(budget_high_signal_low_mask.mean()) >= 0.25),
                    "high_signal_low_budget": bool(float(signal_high_budget_low_mask.mean()) >= 0.25),
                }
            )
        return pd.DataFrame(rows).sort_values(["budget_high_signal_low_days_ratio", "signal_high_budget_low_days_ratio"], ascending=False)

    def _build_daily_alignment_summary(self, *, budget_weights: pd.DataFrame, signal_targets: pd.DataFrame) -> pd.DataFrame:
        budget_share = self._row_share(budget_weights)
        signal_share = self._row_share(signal_targets)
        budget_ranks = budget_weights.rank(axis=1, ascending=False, method="average")
        signal_ranks = signal_targets.rank(axis=1, ascending=False, method="average")
        rows: list[dict[str, Any]] = []
        for dt in budget_weights.index:
            b = budget_weights.loc[dt].fillna(0.0)
            s = signal_targets.loc[dt].fillna(0.0)
            bs = budget_share.loc[dt].fillna(0.0)
            ss = signal_share.loc[dt].fillna(0.0)
            br = budget_ranks.loc[dt]
            sr = signal_ranks.loc[dt]
            rank_corr = br.corr(sr) if br.std() > 0 and sr.std() > 0 else None
            top_budget_3 = set(b.sort_values(ascending=False).head(min(3, len(b))).index)
            top_signal_3 = set(s.sort_values(ascending=False).head(min(3, len(s))).index)
            top_budget_5 = set(b.sort_values(ascending=False).head(min(5, len(b))).index)
            top_signal_5 = set(s.sort_values(ascending=False).head(min(5, len(s))).index)
            budget_top = b >= b.quantile(0.70)
            signal_low = s <= s.quantile(0.30)
            signal_top = s >= s.quantile(0.70)
            budget_low = b <= b.quantile(0.30)
            rows.append(
                {
                    "datetime": dt,
                    "budget_gross": float(b.sum()),
                    "signal_mean": float(s.mean()),
                    "signal_breadth_03": float(s.gt(0.3).mean()),
                    "signal_breadth_06": float(s.gt(0.6).mean()),
                    "budget_signal_rank_corr": None if pd.isna(rank_corr) else float(rank_corr),
                    "budget_signal_share_l1_gap": float((bs - ss).abs().sum()),
                    "top_budget_top_signal_overlap_3": float(len(top_budget_3 & top_signal_3) / max(len(top_budget_3), 1)),
                    "top_budget_top_signal_overlap_5": float(len(top_budget_5 & top_signal_5) / max(len(top_budget_5), 1)),
                    "budget_high_signal_low_count": int((budget_top & signal_low).sum()),
                    "signal_high_budget_low_count": int((signal_top & budget_low).sum()),
                }
            )
        return pd.DataFrame(rows)

    def _build_profile_payload(
        self,
        *,
        state: dict[str, Any],
        split_manifest_path: Path,
        panel_path: Path,
        returns_path: Path,
        asset_summary: pd.DataFrame,
        budget_summary: pd.DataFrame,
        signal_summary: pd.DataFrame,
        alignment: pd.DataFrame,
        daily_alignment: pd.DataFrame,
        correlation: pd.DataFrame,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        benchmark_payload: dict[str, Any],
        warnings: list[str],
    ) -> dict[str, Any]:
        budget_gross = budget_weights.sum(axis=1)
        signal_breadth = signal_targets.gt(0.3).mean(axis=1)
        stacked_corr = self._stacked_corr(budget_weights, signal_targets)
        high_corr_pairs = self._top_correlation_pairs(correlation, top_n=10)
        facts = self._build_fact_hints(
            budget_gross=budget_gross,
            signal_breadth=signal_breadth,
            alignment=alignment,
            daily_alignment=daily_alignment,
            correlation=correlation,
            benchmark_payload=benchmark_payload,
        )
        return {
            "created_at": datetime.now().isoformat(),
            "portfolio_run_id": state.get("portfolio_run_id"),
            "split_manifest_path": self._relative(split_manifest_path),
            "panel_ohlcv_path": self._relative(panel_path),
            "returns_wide_path": self._relative(returns_path),
            "summary": {
                "symbol_count": int(len(budget_weights.columns)),
                "date_count": int(len(budget_weights.index)),
                "start": self._date_text(budget_weights.index.min()),
                "end": self._date_text(budget_weights.index.max()),
                "budget_gross_mean": float(budget_gross.mean()),
                "budget_gross_max": float(budget_gross.max()),
                "signal_mean": float(signal_targets.mean(axis=1).mean()),
                "signal_breadth_gt_03_mean": float(signal_breadth.mean()),
                "budget_signal_stacked_corr": stacked_corr,
                "daily_budget_signal_rank_corr_mean": self._safe_mean(daily_alignment.get("budget_signal_rank_corr")),
                "daily_top5_overlap_mean": self._safe_mean(daily_alignment.get("top_budget_top_signal_overlap_5")),
                "daily_budget_signal_share_l1_gap_mean": self._safe_mean(daily_alignment.get("budget_signal_share_l1_gap")),
                "average_pairwise_correlation": float(correlation.where(~np.eye(len(correlation), dtype=bool)).stack().mean()) if len(correlation) > 1 else None,
            },
            "budget_benchmark": benchmark_payload,
            "top_budget_assets": budget_summary.head(10).to_dict(orient="records"),
            "top_signal_assets": signal_summary.head(10).to_dict(orient="records"),
            "top_budget_signal_conflicts": alignment.sort_values("budget_high_signal_low_days_ratio", ascending=False).head(10).to_dict(orient="records"),
            "top_signal_budget_gaps": alignment.sort_values("signal_high_budget_low_days_ratio", ascending=False).head(10).to_dict(orient="records"),
            "top_share_gap_assets": alignment.reindex(alignment["share_gap_mean"].abs().sort_values(ascending=False).index).head(10).to_dict(orient="records"),
            "daily_alignment_summary": {
                "rank_corr_mean": self._safe_mean(daily_alignment.get("budget_signal_rank_corr")),
                "top3_overlap_mean": self._safe_mean(daily_alignment.get("top_budget_top_signal_overlap_3")),
                "top5_overlap_mean": self._safe_mean(daily_alignment.get("top_budget_top_signal_overlap_5")),
                "share_l1_gap_mean": self._safe_mean(daily_alignment.get("budget_signal_share_l1_gap")),
                "budget_high_signal_low_count_mean": self._safe_mean(daily_alignment.get("budget_high_signal_low_count")),
                "signal_high_budget_low_count_mean": self._safe_mean(daily_alignment.get("signal_high_budget_low_count")),
            },
            "high_correlation_pairs": high_corr_pairs,
            "fact_hints": facts,
            "warnings": warnings,
        }

    def _build_fact_hints(
        self,
        *,
        budget_gross: pd.Series,
        signal_breadth: pd.Series,
        alignment: pd.DataFrame,
        daily_alignment: pd.DataFrame,
        correlation: pd.DataFrame,
        benchmark_payload: dict[str, Any],
    ) -> list[str]:
        hints: list[str] = []
        if float(signal_breadth.mean()) < 0.30:
            hints.append("信号广度偏低，target_gross 不宜过激，需用 max_gross、回撤降仓或换手控制约束风险。")
        if int(alignment["high_budget_low_signal"].sum()) > 0:
            hints.append("存在较多预算高、信号低的资产/日期，组合层需要设计预算折扣或 veto 机制。")
        if int(alignment["high_signal_low_budget"].sum()) > 0:
            hints.append("存在较多信号高、预算低的资产/日期，可研究有限预算软突破、闲置资金再分配或信号增强机制。")
        rank_corr = self._safe_mean(daily_alignment.get("budget_signal_rank_corr"))
        if rank_corr is not None and rank_corr < 0.20:
            hints.append("预算和信号的日频横截面排名相关性偏低，融合策略不宜只依赖单一层，应显式处理两层冲突。")
        top5_overlap = self._safe_mean(daily_alignment.get("top_budget_top_signal_overlap_5"))
        if top5_overlap is not None and top5_overlap < 0.35:
            hints.append("预算 Top5 与信号 Top5 的日均重叠度偏低，组合层需要关注强信号资产是否被预算覆盖。")
        budget_metrics = (benchmark_payload.get("metrics") or {}).get("budget_only") or {}
        equal_metrics = (benchmark_payload.get("metrics") or {}).get("equal_weight_rebalance") or {}
        if budget_metrics and equal_metrics:
            budget_sharpe = self._to_float(budget_metrics.get("sharpe"))
            equal_sharpe = self._to_float(equal_metrics.get("sharpe"))
            if budget_sharpe is not None and equal_sharpe is not None and budget_sharpe < equal_sharpe:
                hints.append("预算层独立 Sharpe 低于等权再平衡，组合层需要谨慎放大预算层权重。")
        if len(correlation) > 1:
            avg_corr = correlation.where(~np.eye(len(correlation), dtype=bool)).stack().mean()
            if pd.notna(avg_corr) and float(avg_corr) > 0.60:
                hints.append("资产平均相关性较高，max_weight 和集中度约束不宜过松。")
        return hints

    def _build_budget_benchmark_profile(self, *, budget_weights: pd.DataFrame, returns: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
        columns = list(returns.columns)
        equal_weights = pd.DataFrame(1.0 / max(len(columns), 1), index=returns.index, columns=columns)
        buy_hold_equity = (1.0 + returns.fillna(0.0)).cumprod()
        buy_hold_curve = buy_hold_equity.mean(axis=1)
        curves = {
            "budget_only": self._portfolio_equity_from_weights(weights=budget_weights, returns=returns),
            "equal_weight_rebalance": self._portfolio_equity_from_weights(weights=equal_weights, returns=returns),
            "equal_weight_buy_hold": buy_hold_curve / buy_hold_curve.iloc[0] if len(buy_hold_curve) else buy_hold_curve,
        }
        equity = pd.DataFrame(curves)
        equity.index.name = "datetime"
        metrics_rows = []
        metrics_payload: dict[str, Any] = {}
        for name, curve in curves.items():
            weights = budget_weights if name == "budget_only" else equal_weights if name == "equal_weight_rebalance" else None
            metrics = self._equity_metrics(curve=curve, weights=weights)
            metrics["name"] = name
            metrics_rows.append(metrics)
            metrics_payload[name] = metrics
        payload = {
            "metrics": metrics_payload,
            "notes": "budget_only 使用预算层最终策略权重独立测算；equal_weight_rebalance 为日频等权再平衡；equal_weight_buy_hold 为初始等权买入持有。",
        }
        return payload, pd.DataFrame(metrics_rows), equity

    def _portfolio_equity_from_weights(self, *, weights: pd.DataFrame, returns: pd.DataFrame) -> pd.Series:
        aligned_weights = weights.reindex(index=returns.index, columns=returns.columns).fillna(0.0)
        aligned_returns = returns.reindex(index=aligned_weights.index, columns=aligned_weights.columns).fillna(0.0)
        daily_returns = (aligned_weights.shift(1).fillna(0.0) * aligned_returns).sum(axis=1)
        return (1.0 + daily_returns).cumprod()

    def _equity_metrics(self, *, curve: pd.Series, weights: pd.DataFrame | None) -> dict[str, Any]:
        curve = pd.to_numeric(curve, errors="coerce").dropna()
        if curve.empty:
            return {}
        returns = curve.pct_change().fillna(0.0)
        total_return = float(curve.iloc[-1] / curve.iloc[0] - 1.0) if curve.iloc[0] else None
        annual_return = (float(curve.iloc[-1] / curve.iloc[0]) ** (252 / max(len(curve), 1)) - 1.0) if curve.iloc[0] else None
        annual_volatility = float(returns.std() * np.sqrt(252)) if len(returns) > 1 else None
        sharpe = float(returns.mean() / returns.std() * np.sqrt(252)) if len(returns) > 1 and returns.std() > 0 else None
        drawdown = curve / curve.cummax() - 1.0
        max_drawdown = float(drawdown.min())
        calmar = float(annual_return / abs(max_drawdown)) if annual_return is not None and max_drawdown < 0 else None
        payload = {
            "total_return": total_return,
            "annual_return": annual_return,
            "annual_volatility": annual_volatility,
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "calmar": calmar,
        }
        if weights is not None:
            aligned = weights.reindex(index=curve.index).fillna(0.0)
            payload["average_gross_exposure"] = float(aligned.sum(axis=1).mean())
            payload["average_holding_count"] = float(aligned.gt(1e-12).sum(axis=1).mean())
            payload["average_daily_turnover"] = float(aligned.diff().abs().sum(axis=1).fillna(0.0).mean())
        return payload

    def _format_markdown(self, profile: dict[str, Any], *, alignment: pd.DataFrame) -> str:
        summary = profile.get("summary", {})
        lines = [
            "# 组合层画像报告",
            "",
            "## 基本摘要",
            "",
            f"- portfolio_run_id: {profile.get('portfolio_run_id')}",
            f"- 日期范围: {summary.get('start')} 至 {summary.get('end')}",
            f"- 交易日数量: {summary.get('date_count')}",
            f"- 资产数量: {summary.get('symbol_count')}",
            f"- 预算层平均总敞口: {self._fmt(summary.get('budget_gross_mean'))}",
            f"- 信号层平均动用比例: {self._fmt(summary.get('signal_mean'))}",
            f"- 信号广度均值(S_i > 0.3): {self._fmt(summary.get('signal_breadth_gt_03_mean'))}",
            f"- 预算/信号整体相关性: {self._fmt(summary.get('budget_signal_stacked_corr'))}",
            f"- 日频预算/信号排名相关性均值: {self._fmt(summary.get('daily_budget_signal_rank_corr_mean'))}",
            f"- 日频预算/信号 Top5 重叠均值: {self._fmt(summary.get('daily_top5_overlap_mean'))}",
            f"- 日频预算/信号份额 L1 差异均值: {self._fmt(summary.get('daily_budget_signal_share_l1_gap_mean'))}",
            "",
            "## 预算层独立表现与基准",
            "",
        ]
        metrics = (profile.get("budget_benchmark") or {}).get("metrics") or {}
        for name in ["budget_only", "equal_weight_rebalance", "equal_weight_buy_hold"]:
            row = metrics.get(name) or {}
            lines.append(
                f"- {name}: total_return={self._fmt(row.get('total_return'))}, "
                f"sharpe={self._fmt(row.get('sharpe'))}, max_drawdown={self._fmt(row.get('max_drawdown'))}"
            )
        lines.extend(
            [
                "",
            "## 事实性提示",
            "",
            ]
        )
        for item in profile.get("fact_hints", []):
            lines.append(f"- {item}")
        if not profile.get("fact_hints"):
            lines.append("- 未触发明显异常提示。")
        lines.extend(["", "## 高预算资产", ""])
        lines.extend(self._records_table(profile.get("top_budget_assets", []), ["symbol", "budget_mean", "budget_zero_ratio"]))
        lines.extend(["", "## 高信号资产", ""])
        lines.extend(self._records_table(profile.get("top_signal_assets", []), ["symbol", "signal_mean", "signal_gt_03_ratio"]))
        lines.extend(["", "## 预算/信号错配资产", ""])
        mismatch = alignment.loc[alignment["high_budget_low_signal"] | alignment["high_signal_low_budget"]].head(12)
        lines.extend(
            self._records_table(
                mismatch.to_dict(orient="records"),
                [
                    "symbol",
                    "budget_share_mean",
                    "signal_share_mean",
                    "share_gap_mean",
                    "budget_high_signal_low_days_ratio",
                    "signal_high_budget_low_days_ratio",
                    "alignment_type",
                ],
            )
        )
        lines.extend(["", "## 高相关资产对", ""])
        lines.extend(self._records_table(profile.get("high_correlation_pairs", []), ["symbol_a", "symbol_b", "correlation"]))
        if profile.get("warnings"):
            lines.extend(["", "## Warnings", ""])
            for warning in profile["warnings"]:
                lines.append(f"- {warning}")
        lines.append("")
        return "\n".join(lines)

    def _write_charts(
        self,
        *,
        output_dir: Path,
        alignment: pd.DataFrame,
        daily_alignment: pd.DataFrame,
        correlation: pd.DataFrame,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        benchmark_equity: pd.DataFrame,
    ) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            scatter_path = output_dir / "budget_signal_scatter.png"
            fig, ax = plt.subplots(figsize=(8, 6))
            ax.scatter(alignment["budget_mean"], alignment["signal_mean"], s=42, alpha=0.75)
            for _, row in alignment.iterrows():
                ax.annotate(str(row["symbol"]), (row["budget_mean"], row["signal_mean"]), fontsize=8, alpha=0.75)
            ax.set_xlabel("Mean budget weight R")
            ax.set_ylabel("Mean signal target S")
            ax.set_title("Budget vs Signal Alignment")
            fig.tight_layout()
            fig.savefig(scatter_path, dpi=140)
            plt.close(fig)
            paths["budget_signal_scatter"] = scatter_path

            gross_path = output_dir / "gross_exposure_comparison.png"
            fig, ax = plt.subplots(figsize=(10, 5))
            budget_weights.sum(axis=1).plot(ax=ax, label="budget gross", linewidth=1.2)
            signal_targets.mean(axis=1).plot(ax=ax, label="signal mean", linewidth=1.2)
            ax.set_title("Gross Exposure Comparison")
            ax.legend()
            fig.tight_layout()
            fig.savefig(gross_path, dpi=140)
            plt.close(fig)
            paths["gross_exposure_comparison"] = gross_path

            benchmark_path = output_dir / "budget_vs_benchmarks.png"
            fig, ax = plt.subplots(figsize=(10, 5))
            benchmark_equity.plot(ax=ax, linewidth=1.2)
            ax.set_title("Budget Strategy vs Benchmarks")
            ax.legend()
            fig.tight_layout()
            fig.savefig(benchmark_path, dpi=140)
            plt.close(fig)
            paths["budget_vs_benchmarks"] = benchmark_path

            alignment_path = output_dir / "daily_alignment_timeseries.png"
            fig, ax = plt.subplots(figsize=(10, 5))
            frame = daily_alignment.set_index("datetime") if "datetime" in daily_alignment.columns else daily_alignment
            for column in ["budget_signal_rank_corr", "top_budget_top_signal_overlap_5", "signal_breadth_03"]:
                if column in frame.columns:
                    frame[column].plot(ax=ax, label=column, linewidth=1.1)
            ax.set_title("Daily Budget/Signal Alignment")
            ax.legend()
            fig.tight_layout()
            fig.savefig(alignment_path, dpi=140)
            plt.close(fig)
            paths["daily_alignment_timeseries"] = alignment_path

            rank_gap_path = output_dir / "budget_signal_rank_gap_bar.png"
            fig, ax = plt.subplots(figsize=(10, 5))
            plot_data = alignment.sort_values("pct_rank_gap_mean")["pct_rank_gap_mean"]
            plot_data.index = alignment.sort_values("pct_rank_gap_mean")["symbol"]
            plot_data.plot(kind="bar", ax=ax)
            ax.axhline(0, color="black", linewidth=0.8)
            ax.set_title("Mean Signal/Budget Percentile Rank Gap")
            fig.tight_layout()
            fig.savefig(rank_gap_path, dpi=140)
            plt.close(fig)
            paths["budget_signal_rank_gap_bar"] = rank_gap_path

            corr_path = output_dir / "correlation_heatmap.png"
            fig, ax = plt.subplots(figsize=(10, 8))
            im = ax.imshow(correlation.values, vmin=-1, vmax=1, cmap="coolwarm")
            ax.set_xticks(range(len(correlation.columns)))
            ax.set_yticks(range(len(correlation.index)))
            ax.set_xticklabels(correlation.columns, rotation=90, fontsize=7)
            ax.set_yticklabels(correlation.index, fontsize=7)
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
            ax.set_title("Return Correlation Matrix")
            fig.tight_layout()
            fig.savefig(corr_path, dpi=140)
            plt.close(fig)
            paths["correlation_heatmap"] = corr_path
        except Exception:
            return paths
        return paths

    def _update_run_state(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        profile: dict[str, Any],
        output_dir: Path,
        profile_json_path: Path,
        profile_md_path: Path,
        alignment_path: Path,
        daily_alignment_path: Path,
        benchmark_metrics_json_path: Path,
        benchmark_metrics_csv_path: Path,
        benchmark_equity_path: Path,
        daily_budget_weights_path: Path,
        daily_signal_targets_path: Path,
        chart_paths: dict[str, Path],
    ) -> None:
        now = datetime.now().isoformat()
        profile_payload = {
            "status": "success",
            "profile_dir": self._relative(output_dir),
            "portfolio_profile_path": self._relative(profile_json_path),
            "portfolio_profile_md_path": self._relative(profile_md_path),
            "budget_signal_alignment_path": self._relative(alignment_path),
            "daily_budget_signal_alignment_path": self._relative(daily_alignment_path),
            "budget_benchmark_metrics_json_path": self._relative(benchmark_metrics_json_path),
            "budget_benchmark_metrics_csv_path": self._relative(benchmark_metrics_csv_path),
            "budget_benchmark_equity_path": self._relative(benchmark_equity_path),
            "daily_budget_weights_path": self._relative(daily_budget_weights_path),
            "daily_signal_targets_path": self._relative(daily_signal_targets_path),
            "charts": {key: self._relative(path) for key, path in chart_paths.items()},
            "summary": profile.get("summary", {}),
            "updated_at": now,
        }
        state["profile"] = profile_payload
        state.setdefault("artifacts", {}).setdefault("profiles", {})["portfolio_profile"] = profile_payload
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "PortfolioProfileService",
                "event": "portfolio_profile_completed",
                "summary": "组合层画像已生成，PortfolioAgent 可以据此创建 v001_initial_fusion。",
                "profile_path": self._relative(profile_json_path),
                "profile_md_path": self._relative(profile_md_path),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _resolve_signal_params(self, *, run_state_path: Path, param_space_path: Path) -> dict[str, Any]:
        params = self._params_from_param_space(param_space_path)
        if run_state_path.exists():
            try:
                state = self._read_json(run_state_path)
                selected = (
                    state.get("steps", {}).get("final_selection", {}).get("selected_attempt_id")
                    or state.get("steps", {}).get("strategy_search", {}).get("best_attempt_id")
                )
                for attempt in state.get("attempts", []):
                    if attempt.get("attempt_id") == selected and isinstance(attempt.get("best_params"), dict):
                        params.update(attempt["best_params"])
                        break
            except Exception:
                pass
        return params

    def _params_from_param_space(self, param_space_path: Path) -> dict[str, Any]:
        if not param_space_path.exists():
            return {}
        try:
            param_space = self._read_json(param_space_path)
        except Exception:
            return {}
        params: dict[str, Any] = {}
        for key, spec in param_space.items():
            if isinstance(spec, dict) and "default" in spec:
                params[key] = spec["default"]
        return params

    def _resolve_strategy_class_name(self, strategy_meta_path: Path) -> str:
        if strategy_meta_path.exists():
            meta = self._read_json(strategy_meta_path)
            return str(meta.get("strategy_class_name") or "Strategy")
        return "Strategy"

    def _load_strategy_class(self, strategy_path: Path, class_name: str) -> type:
        module = self._load_module(strategy_path)
        strategy_cls = getattr(module, class_name, None)
        if strategy_cls is None:
            raise AttributeError(f"{strategy_path} 中没有策略类 {class_name}。")
        if not hasattr(strategy_cls, "suggest"):
            raise TypeError(f"{strategy_path}:{class_name} 缺少 suggest 方法。")
        return strategy_cls

    def _load_module(self, path: Path) -> ModuleType:
        module_name = f"portfolio_profile_signal_{path.stem}_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载策略脚本：{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _resolve_split_manifest_path(self, split_manifest_path: Path | None, *, state: dict[str, Any]) -> Path:
        if split_manifest_path:
            return self._resolve_path(split_manifest_path)
        raw = state.get("data", {}).get("split_manifest") or state.get("artifacts", {}).get("datasets", {}).get("portfolio_splits", {}).get("manifest_path")
        if not raw:
            raise ValueError("未传 split_manifest_path，且 portfolio_run_state.json 中没有 data.split_manifest。")
        return self._resolve_path(raw)

    def _resolve_data_paths(self, split_manifest: dict[str, Any]) -> tuple[Path, Path]:
        panel_raw = split_manifest.get("full_panel_path") or split_manifest.get("source_panel_ohlcv_path")
        returns_raw = split_manifest.get("full_returns_path") or split_manifest.get("source_returns_wide_path")
        if not panel_raw or not returns_raw:
            raise ValueError("split_manifest.json 缺少 full/source panel 或 returns 路径。")
        return self._resolve_path(panel_raw), self._resolve_path(returns_raw)

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        root = state.get("directories", {}).get("root")
        if not root:
            raise ValueError("portfolio_run_state.json 缺少 directories.root。")
        return self._resolve_path(Path(root) / "profile")

    def _load_panel(self, panel_path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        if not panel_path.exists():
            raise FileNotFoundError(f"panel_ohlcv 文件不存在：{panel_path}")
        panel = pd.read_parquet(panel_path)
        required = {"datetime", "symbol", "close"}
        missing = required - set(panel.columns)
        if missing:
            raise ValueError(f"panel_ohlcv 缺少字段：{sorted(missing)}")
        panel = panel.copy()
        panel["datetime"] = pd.to_datetime(panel["datetime"], errors="coerce").dt.normalize()
        panel["symbol"] = normalize_symbol_series(panel["symbol"], reference_symbols=reference_symbols)
        panel = panel.dropna(subset=["datetime", "symbol"]).sort_values(["symbol", "datetime"]).reset_index(drop=True)
        return panel

    def _load_wide_frame(self, path: Path, *, reference_symbols: list[str] | None = None) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        return load_wide_parquet(path, reference_symbols=reference_symbols)

    def _reference_symbols_from_state(self, state: dict[str, Any]) -> list[str]:
        symbols = state.get("source_artifacts", {}).get("signals", {}).get("symbols")
        if isinstance(symbols, list):
            return sorted({str(item).upper() for item in symbols if item})
        return []

    def _read_json(self, path: str | Path) -> dict[str, Any]:
        return json.loads(self._resolve_path(path).read_text(encoding="utf-8-sig"))

    def _resolve_optional_path(self, path: str | Path | None, *, default: Path) -> Path:
        if path:
            return self._resolve_path(path)
        return self._resolve_path(default)

    def _resolve_path(self, path: str | Path | None) -> Path:
        if path is None:
            raise ValueError("路径不能为空。")
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: str | Path | None) -> str | None:
        if path is None:
            return None
        value = self._resolve_path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)

    @staticmethod
    def _date_text(value: Any) -> str | None:
        if value is None or pd.isna(value):
            return None
        return str(pd.to_datetime(value).date())

    @staticmethod
    def _safe_mean(series: Any) -> float | None:
        if series is None:
            return None
        value = pd.to_numeric(series, errors="coerce").mean()
        return None if pd.isna(value) else float(value)

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(result) else result

    @staticmethod
    def _row_share(frame: pd.DataFrame) -> pd.DataFrame:
        totals = frame.sum(axis=1).replace(0.0, np.nan)
        return frame.div(totals, axis=0).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    @staticmethod
    def _safe_mask_mean(series: pd.Series, mask: pd.Series) -> float | None:
        aligned_mask = mask.reindex(series.index).fillna(False)
        if not bool(aligned_mask.any()):
            return None
        value = pd.to_numeric(series.loc[aligned_mask], errors="coerce").mean()
        return None if pd.isna(value) else float(value)

    @staticmethod
    def _classify_alignment(
        *,
        share_gap: float,
        rank_gap: float,
        budget_high_signal_low_ratio: float,
        signal_high_budget_low_ratio: float,
        active_ratio: float,
    ) -> str:
        if active_ratio < 0.10:
            return "low_signal_activity"
        if budget_high_signal_low_ratio >= 0.25:
            return "budget_leads_signal"
        if signal_high_budget_low_ratio >= 0.25:
            return "signal_underfunded"
        if abs(share_gap) <= 0.02 and abs(rank_gap) <= 1.0:
            return "aligned_core"
        if share_gap > 0:
            return "signal_share_above_budget"
        return "budget_share_above_signal"

    @staticmethod
    def _stacked_corr(budget_weights: pd.DataFrame, signal_targets: pd.DataFrame) -> float | None:
        b = budget_weights.stack()
        s = signal_targets.stack()
        aligned = pd.concat([b.rename("budget"), s.rename("signal")], axis=1).dropna()
        if aligned.empty or aligned["budget"].std() <= 0 or aligned["signal"].std() <= 0:
            return None
        value = aligned["budget"].corr(aligned["signal"])
        return None if pd.isna(value) else float(value)

    @staticmethod
    def _top_correlation_pairs(correlation: pd.DataFrame, *, top_n: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        columns = list(correlation.columns)
        for i, left in enumerate(columns):
            for right in columns[i + 1 :]:
                value = correlation.loc[left, right]
                if pd.isna(value):
                    continue
                rows.append({"symbol_a": left, "symbol_b": right, "correlation": float(value)})
        return sorted(rows, key=lambda item: abs(item["correlation"]), reverse=True)[:top_n]

    @staticmethod
    def _fmt(value: Any) -> str:
        if value is None or pd.isna(value):
            return "N/A"
        if isinstance(value, (int, np.integer)):
            return str(int(value))
        if isinstance(value, (float, np.floating)):
            return f"{float(value):.6f}"
        return str(value)

    def _records_table(self, records: list[dict[str, Any]], columns: list[str]) -> list[str]:
        if not records:
            return ["无。"]
        lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
        for record in records:
            lines.append("| " + " | ".join(self._fmt(record.get(column)) for column in columns) + " |")
        return lines
