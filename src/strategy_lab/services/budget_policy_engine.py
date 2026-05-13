from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_run import BudgetRunManager
from strategy_lab.services.data_format import load_wide_parquet, normalize_symbol_series


class BudgetPolicyEngineRequest(BaseModel):
    budget_run_state_path: Path
    policy_config_path: Path
    output_dir: Path | None = None
    panel_ohlcv_path: Path | None = None
    returns_wide_path: Path | None = None
    policy_id: str | None = None
    update_run_state: bool = True


class BudgetPolicyEngineResult(BaseModel):
    budget_run_state_path: Path
    policy_config_path: Path
    output_dir: Path
    policy_id: str
    daily_budget_weights_path: Path
    daily_scores_path: Path
    gate_results_path: Path
    raw_weights_path: Path
    turnover_path: Path
    manifest_path: Path
    diagnostics_path: Path
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class BudgetPolicyEngine:
    """预算层结构化策略执行器。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetPolicyEngineRequest) -> BudgetPolicyEngineResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        policy_config_path = self._resolve_path(request.policy_config_path)
        policy_config = json.loads(policy_config_path.read_text(encoding="utf-8"))
        self._validate_policy_config(policy_config)

        panel_path = self._resolve_panel_path(state, request.panel_ohlcv_path)
        returns_path = self._resolve_returns_path(state, request.returns_wide_path)
        reference_symbols = self._reference_symbols_from_state(state)
        panel = self._load_panel(panel_path, reference_symbols=reference_symbols)
        returns = self._load_returns(returns_path, reference_symbols=reference_symbols)
        close = panel.pivot(index="datetime", columns="symbol", values="close").sort_index()
        volume = panel.pivot(index="datetime", columns="symbol", values="volume").sort_index() if "volume" in panel.columns else None
        returns = returns.reindex(close.index).sort_index()
        symbols = [str(symbol) for symbol in close.columns]

        metadata = self._load_metadata(state)
        signal_records = self._load_signal_records(state)
        policy_id = request.policy_id or self._make_policy_id(policy_config, policy_config_path)
        output_dir = self._resolve_output_dir(request.output_dir, state=state, policy_id=policy_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        diagnostics_enabled = bool(policy_config.get("diagnostics", {}).get("enabled", True))
        weights_rows: list[pd.Series] = []
        raw_rows: list[pd.Series] = []
        score_rows: list[pd.Series] = []
        gate_rows: list[dict[str, Any]] = []
        turnover_rows: list[dict[str, Any]] = []
        constraint_events: list[dict[str, Any]] = []
        warnings: list[str] = []

        prev_weights = pd.Series(0.0, index=symbols, dtype=float)
        last_rebalance_idx: int | None = None
        for idx, date in enumerate(close.index):
            history_close = close.iloc[: idx + 1]
            history_returns = returns.iloc[: idx + 1]
            history_volume = volume.iloc[: idx + 1] if volume is not None else None

            gate_result = self._evaluate_gates(
                policy_config.get("universe_gate", {}),
                date=date,
                close_history=history_close,
                returns_history=history_returns,
                volume_history=history_volume,
                metadata=metadata,
                signal_records=signal_records,
            )
            for symbol, item in gate_result.items():
                gate_rows.append(
                    {
                        "datetime": date,
                        "symbol": symbol,
                        "passed": bool(item["passed"]),
                        "reasons": "; ".join(item["reasons"]),
                    }
                )

            scores = self._score_assets(
                policy_config.get("asset_scorer", {}),
                date=date,
                close_history=history_close,
                returns_history=history_returns,
                gate_result=gate_result,
                signal_records=signal_records,
            )
            raw_weights = self._allocate(
                policy_config.get("allocation_engine", {}),
                date=date,
                scores=scores,
                close_history=history_close,
                returns_history=history_returns,
                policy_config=policy_config,
            )
            raw_weights = raw_weights.reindex(symbols).fillna(0.0)
            overlay_weights = self._apply_risk_overlays(
                policy_config.get("risk_overlay", {}),
                date=date,
                weights=raw_weights,
                prev_weights=prev_weights,
                returns_history=history_returns,
                policy_config=policy_config,
            )
            constrained = self._project_constraints(
                policy_config.get("constraint_projector", {}),
                overlay_weights,
                unavailable=[symbol for symbol, item in gate_result.items() if not item["passed"]],
                symbols=symbols,
            )
            if constrained.attrs.get("constraint_events"):
                for event in constrained.attrs["constraint_events"]:
                    constraint_events.append({"datetime": str(pd.Timestamp(date).date()), **event})

            should_rebalance = self._should_rebalance(
                policy_config.get("rebalance_scheduler", {}),
                idx=idx,
                date=date,
                proposed=constrained,
                prev_weights=prev_weights,
                last_rebalance_idx=last_rebalance_idx,
                returns_history=history_returns,
            )
            if should_rebalance:
                final_weights = constrained
                last_rebalance_idx = idx
            else:
                final_weights = self._project_constraints(
                    policy_config.get("constraint_projector", {}),
                    prev_weights,
                    unavailable=[symbol for symbol, item in gate_result.items() if not item["passed"]],
                    symbols=symbols,
                )

            turnover = float((final_weights - prev_weights).abs().sum())
            turnover_rows.append(
                {
                    "datetime": date,
                    "turnover": turnover,
                    "rebalanced": bool(should_rebalance),
                    "holding_count": int((final_weights > 0).sum()),
                    "gross_exposure": float(final_weights.sum()),
                }
            )
            score_rows.append(scores.reindex(symbols).rename(date))
            raw_rows.append(raw_weights.reindex(symbols).rename(date))
            weights_rows.append(final_weights.reindex(symbols).rename(date))
            prev_weights = final_weights.reindex(symbols).fillna(0.0)

        weights = pd.DataFrame(weights_rows)
        raw_weights_df = pd.DataFrame(raw_rows)
        scores_df = pd.DataFrame(score_rows)
        gate_df = pd.DataFrame(gate_rows)
        turnover_df = pd.DataFrame(turnover_rows)
        for df in [weights, raw_weights_df, scores_df]:
            df.index.name = "datetime"
        weights = weights.fillna(0.0)
        raw_weights_df = raw_weights_df.fillna(0.0)
        scores_df = scores_df.fillna(0.0)

        weights_path = output_dir / "daily_budget_weights.parquet"
        scores_path = output_dir / "daily_scores.parquet"
        gate_path = output_dir / "gate_results.parquet"
        raw_path = output_dir / "raw_weights.parquet"
        turnover_path = output_dir / "turnover.parquet"
        diagnostics_path = output_dir / "diagnostics.json"
        manifest_path = output_dir / "policy_execution_manifest.json"

        weights.to_parquet(weights_path)
        scores_df.to_parquet(scores_path)
        gate_df.to_parquet(gate_path, index=False)
        raw_weights_df.to_parquet(raw_path)
        turnover_df.to_parquet(turnover_path, index=False)

        diagnostics = {
            "enabled": diagnostics_enabled,
            "constraint_events": constraint_events[:500],
            "constraint_event_count": len(constraint_events),
            "warnings": warnings,
        }
        diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        summary = self._build_summary(weights=weights, turnover=turnover_df, policy_config=policy_config)
        manifest = {
            "created_at": datetime.now().isoformat(),
            "budget_run_id": state.get("budget_run_id"),
            "policy_id": policy_id,
            "policy_name": policy_config.get("policy_name"),
            "policy_config_path": str(self._relative(policy_config_path)),
            "panel_ohlcv_path": str(self._relative(panel_path)),
            "returns_wide_path": str(self._relative(returns_path)),
            "output_dir": str(self._relative(output_dir)),
            "daily_budget_weights_path": str(self._relative(weights_path)),
            "daily_scores_path": str(self._relative(scores_path)),
            "gate_results_path": str(self._relative(gate_path)),
            "raw_weights_path": str(self._relative(raw_path)),
            "turnover_path": str(self._relative(turnover_path)),
            "diagnostics_path": str(self._relative(diagnostics_path)),
            "summary": summary,
            "warnings": warnings,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                policy_id=policy_id,
                manifest=manifest,
                manifest_path=manifest_path,
            )

        return BudgetPolicyEngineResult(
            budget_run_state_path=state_path,
            policy_config_path=policy_config_path,
            output_dir=output_dir,
            policy_id=policy_id,
            daily_budget_weights_path=weights_path,
            daily_scores_path=scores_path,
            gate_results_path=gate_path,
            raw_weights_path=raw_path,
            turnover_path=turnover_path,
            manifest_path=manifest_path,
            diagnostics_path=diagnostics_path,
            summary=summary,
            warnings=warnings,
        )

    def _evaluate_gates(
        self,
        config: dict[str, Any],
        *,
        date: pd.Timestamp,
        close_history: pd.DataFrame,
        returns_history: pd.DataFrame,
        volume_history: pd.DataFrame | None,
        metadata: dict[str, dict[str, Any]],
        signal_records: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        result = {symbol: {"passed": True, "reasons": []} for symbol in close_history.columns}
        for gate in config.get("gates", []):
            gate_type = gate.get("type")
            params = dict(gate.get("params") or {})
            for symbol in close_history.columns:
                passed, reason = self._evaluate_single_gate(
                    gate_type,
                    params,
                    symbol=symbol,
                    date=date,
                    close_history=close_history[symbol],
                    returns_history=returns_history[symbol] if symbol in returns_history else pd.Series(dtype=float),
                    volume_history=volume_history[symbol] if volume_history is not None and symbol in volume_history else None,
                    metadata=metadata.get(symbol, {}),
                    signal_record=signal_records.get(symbol, {}),
                )
                if not passed:
                    result[symbol]["passed"] = False
                    result[symbol]["reasons"].append(reason)
        return result

    def _evaluate_single_gate(
        self,
        gate_type: str,
        params: dict[str, Any],
        *,
        symbol: str,
        date: pd.Timestamp,
        close_history: pd.Series,
        returns_history: pd.Series,
        volume_history: pd.Series | None,
        metadata: dict[str, Any],
        signal_record: dict[str, Any],
    ) -> tuple[bool, str]:
        close_valid = close_history.dropna()
        current_close = close_history.iloc[-1] if len(close_history) else np.nan
        if gate_type == "data_availability_gate":
            min_history_days = int(params.get("min_history_days", 120))
            max_recent_missing_days = int(params.get("max_recent_missing_days", 0))
            require_current_bar = bool(params.get("require_current_bar", True))
            if require_current_bar and pd.isna(current_close):
                return False, "当日无行情"
            if len(close_valid) < min_history_days:
                return False, f"历史数据不足 {len(close_valid)} < {min_history_days}"
            if max_recent_missing_days >= 0:
                recent = close_history.tail(max_recent_missing_days + 1)
                if int(recent.isna().sum()) > max_recent_missing_days:
                    return False, "近期缺失过多"
            return True, ""
        if gate_type == "absolute_momentum_gate":
            window = int(params.get("window", 60))
            threshold = float(params.get("threshold", 0.0))
            if pd.isna(current_close) or len(close_valid) <= window:
                return False, f"绝对动量窗口不足 {window}"
            past = close_valid.iloc[-window - 1]
            momentum = current_close / past - 1.0 if past else np.nan
            return bool(pd.notna(momentum) and momentum > threshold), f"absolute_momentum={momentum:.6f} <= {threshold}"
        if gate_type == "trend_filter_gate":
            ma_window = int(params.get("ma_window", 120))
            slope_window = int(params.get("slope_window", 20))
            min_slope = float(params.get("min_slope", 0.0))
            require_price_above_ma = bool(params.get("require_price_above_ma", True))
            if len(close_valid) < ma_window + slope_window:
                return False, "趋势过滤窗口不足"
            ma = close_valid.rolling(ma_window).mean().dropna()
            current_ma = ma.iloc[-1]
            past_ma = ma.iloc[-slope_window - 1] if len(ma) > slope_window else np.nan
            slope = current_ma / past_ma - 1.0 if pd.notna(past_ma) and past_ma else np.nan
            if require_price_above_ma and current_close <= current_ma:
                return False, "价格低于均线"
            return bool(pd.notna(slope) and slope >= min_slope), f"slope={slope:.6f} < {min_slope}"
        if gate_type == "risk_filter_gate":
            vol_window = int(params.get("vol_window", 60))
            max_annual_vol = float(params.get("max_annual_vol", 0.45))
            drawdown_window = int(params.get("drawdown_window", 120))
            max_drawdown_limit = float(params.get("max_drawdown_limit", -0.25))
            ret = returns_history.dropna().tail(vol_window)
            if len(ret) >= max(10, vol_window // 3):
                vol = float(ret.std(ddof=0) * math.sqrt(252))
                if vol > max_annual_vol:
                    return False, f"vol={vol:.6f} > {max_annual_vol}"
            price = close_valid.tail(drawdown_window)
            if len(price) >= max(10, drawdown_window // 3):
                dd = float((price / price.cummax() - 1.0).min())
                if dd < max_drawdown_limit:
                    return False, f"drawdown={dd:.6f} < {max_drawdown_limit}"
            return True, ""
        if gate_type == "liquidity_filter_gate":
            if volume_history is None:
                return True, ""
            volume_window = int(params.get("volume_window", 20))
            min_avg_volume = params.get("min_avg_volume")
            if min_avg_volume is None:
                return True, ""
            avg_volume = volume_history.dropna().tail(volume_window).mean()
            return bool(pd.notna(avg_volume) and avg_volume >= float(min_avg_volume)), f"avg_volume={avg_volume} < {min_avg_volume}"
        if gate_type == "metadata_filter_gate":
            include_types = set(params.get("include_asset_types") or [])
            exclude_types = set(params.get("exclude_asset_types") or [])
            include_markets = set(params.get("include_markets") or [])
            exclude_symbols = set(str(item).upper() for item in params.get("exclude_symbols") or [])
            asset_type = metadata.get("asset_type")
            market = metadata.get("market")
            if symbol.upper() in exclude_symbols:
                return False, "用户排除资产"
            if include_types and asset_type not in include_types:
                return False, "资产类型不在允许列表"
            if exclude_types and asset_type in exclude_types:
                return False, "资产类型在排除列表"
            if include_markets and market not in include_markets:
                return False, "市场不在允许列表"
            return True, ""
        if gate_type == "signal_quality_gate":
            if bool(params.get("require_final_strategy", True)) and not signal_record:
                return False, "缺少信号层最终策略记录"
            metrics = signal_record.get("metrics") or signal_record.get("final_metrics") or {}
            min_sharpe = params.get("min_signal_sharpe")
            max_dd = params.get("max_signal_drawdown")
            if min_sharpe is not None and metrics.get("sharpe") is not None and float(metrics["sharpe"]) < float(min_sharpe):
                return False, "信号层 Sharpe 不足"
            if max_dd is not None and metrics.get("max_drawdown") is not None and float(metrics["max_drawdown"]) < float(max_dd):
                return False, "信号层回撤过大"
            return True, ""
        raise ValueError(f"不支持的 UniverseGate 类型：{gate_type}")

    def _score_assets(
        self,
        config: dict[str, Any],
        *,
        date: pd.Timestamp,
        close_history: pd.DataFrame,
        returns_history: pd.DataFrame,
        gate_result: dict[str, dict[str, Any]],
        signal_records: dict[str, dict[str, Any]],
    ) -> pd.Series:
        symbols = list(close_history.columns)
        total = pd.Series(0.0, index=symbols, dtype=float)
        scorers = list(config.get("scorers") or [])
        normalization = str(config.get("normalization") or "rank_pct")
        if not scorers:
            passed = [symbol for symbol in symbols if gate_result.get(symbol, {}).get("passed")]
            total.loc[passed] = 1.0
            return total
        weight_sum = sum(abs(float(item.get("weight", 1.0))) for item in scorers) or 1.0
        for scorer in scorers:
            scorer_type = scorer.get("type")
            params = dict(scorer.get("params") or {})
            weight = float(scorer.get("weight", 1.0)) / weight_sum
            raw = self._compute_scorer(scorer_type, params, close_history=close_history, returns_history=returns_history, signal_records=signal_records)
            normalized = self._normalize_scores(raw, method=normalization)
            total = total.add(normalized * weight, fill_value=0.0)
        for symbol, item in gate_result.items():
            if not item.get("passed"):
                total.loc[symbol] = 0.0
        return total.fillna(0.0)

    def _compute_scorer(
        self,
        scorer_type: str,
        params: dict[str, Any],
        *,
        close_history: pd.DataFrame,
        returns_history: pd.DataFrame,
        signal_records: dict[str, dict[str, Any]],
    ) -> pd.Series:
        symbols = list(close_history.columns)
        if scorer_type == "relative_momentum":
            window = int(params.get("window", 60))
            return self._momentum(close_history, window)
        if scorer_type == "multi_window_momentum":
            windows = list(params.get("windows") or [20, 60, 120])
            weights = list(params.get("weights") or [1 / len(windows)] * len(windows))
            total = pd.Series(0.0, index=symbols, dtype=float)
            weight_sum = sum(abs(float(weight)) for weight in weights) or 1.0
            for window, weight in zip(windows, weights, strict=False):
                total = total.add(self._momentum(close_history, int(window)) * (float(weight) / weight_sum), fill_value=0.0)
            return total
        if scorer_type == "risk_adjusted_momentum":
            momentum_window = int(params.get("momentum_window", 60))
            vol_window = int(params.get("vol_window", 20))
            vol_floor = float(params.get("vol_floor", 0.03))
            momentum = self._momentum(close_history, momentum_window)
            vol = returns_history.tail(vol_window).std(ddof=0) * math.sqrt(252)
            return momentum / vol.clip(lower=vol_floor)
        if scorer_type == "trend_quality":
            window = int(params.get("window", 60))
            method = str(params.get("method") or "composite")
            recent = close_history.tail(window)
            if method == "up_day_ratio":
                return recent.pct_change(fill_method=None).gt(0).mean()
            ma = recent.rolling(max(5, window // 4)).mean()
            slope = ma.iloc[-1] / ma.iloc[0] - 1.0 if len(ma.dropna()) > 1 else pd.Series(0.0, index=symbols)
            up_ratio = recent.pct_change(fill_method=None).gt(0).mean()
            return slope.fillna(0.0) * 0.6 + up_ratio.fillna(0.0) * 0.4
        if scorer_type == "low_corr_bonus":
            corr_window = int(params.get("corr_window", 120))
            corr = returns_history.tail(corr_window).corr(min_periods=max(10, corr_window // 3))
            avg_corr = corr.replace(1.0, np.nan).mean(axis=1)
            return -avg_corr.reindex(symbols).fillna(0.0)
        if scorer_type == "inverse_vol_preference":
            vol_window = int(params.get("vol_window", 60))
            vol_floor = float(params.get("vol_floor", 0.03))
            vol = returns_history.tail(vol_window).std(ddof=0) * math.sqrt(252)
            return 1.0 / vol.clip(lower=vol_floor)
        if scorer_type == "drawdown_resilience":
            drawdown_window = int(params.get("drawdown_window", 120))
            price = close_history.tail(drawdown_window)
            dd = price / price.cummax() - 1.0
            return -dd.min().reindex(symbols).fillna(0.0)
        if scorer_type == "signal_quality_bonus":
            return pd.Series({symbol: self._signal_quality_score(signal_records.get(symbol, {}), params) for symbol in symbols}, dtype=float)
        raise ValueError(f"不支持的 AssetScorer 类型：{scorer_type}")

    def _allocate(
        self,
        config: dict[str, Any],
        *,
        date: pd.Timestamp,
        scores: pd.Series,
        close_history: pd.DataFrame,
        returns_history: pd.DataFrame,
        policy_config: dict[str, Any],
    ) -> pd.Series:
        engine_type = config.get("type")
        params = dict(config.get("params") or {})
        positive = scores.clip(lower=0.0).fillna(0.0)
        if engine_type == "topk_equal":
            top_k = int(params.get("top_k", 4))
            selected = positive.sort_values(ascending=False).head(top_k)
            selected = selected[selected > 0]
            if selected.empty:
                return pd.Series(0.0, index=scores.index)
            return pd.Series(1.0 / len(selected), index=selected.index).reindex(scores.index).fillna(0.0)
        if engine_type == "topk_score_weighted":
            top_k = int(params.get("top_k", 4))
            score_floor = float(params.get("score_floor", 0.0))
            power = float(params.get("power", 1.0))
            selected = positive[positive > score_floor].sort_values(ascending=False).head(top_k)
            if selected.empty:
                return pd.Series(0.0, index=scores.index)
            raw = selected.pow(power)
            return (raw / raw.sum()).reindex(scores.index).fillna(0.0)
        if engine_type == "softmax_weighted":
            temperature = max(float(params.get("temperature", 0.8)), 1e-6)
            top_k = params.get("top_k")
            values = scores.copy().fillna(0.0)
            if top_k is not None:
                keep = values.sort_values(ascending=False).head(int(top_k)).index
                values = values.where(values.index.isin(keep), np.nan)
            exp = np.exp((values - values.max()) / temperature).replace([np.inf, -np.inf], np.nan).dropna()
            if exp.empty or exp.sum() <= 0:
                return pd.Series(0.0, index=scores.index)
            return (exp / exp.sum()).reindex(scores.index).fillna(0.0)
        if engine_type in {"inverse_vol", "risk_parity_simple"}:
            vol_window = int(params.get("vol_window", 60))
            vol_floor = float(params.get("vol_floor", 0.03))
            top_k = params.get("top_k")
            candidates = positive[positive > 0]
            if top_k is not None:
                candidates = candidates.sort_values(ascending=False).head(int(top_k))
            vol = returns_history[candidates.index].tail(vol_window).std(ddof=0) * math.sqrt(252) if not candidates.empty else pd.Series(dtype=float)
            raw = 1.0 / vol.clip(lower=vol_floor)
            if raw.empty or raw.sum() <= 0:
                return pd.Series(0.0, index=scores.index)
            return (raw / raw.sum()).reindex(scores.index).fillna(0.0)
        if engine_type == "score_vol_blend":
            alpha = float(params.get("alpha", 1.0))
            beta = float(params.get("beta", 1.0))
            vol_window = int(params.get("vol_window", 60))
            top_k = int(params.get("top_k", 4))
            candidates = positive.sort_values(ascending=False).head(top_k)
            candidates = candidates[candidates > 0]
            vol = returns_history[candidates.index].tail(vol_window).std(ddof=0) * math.sqrt(252) if not candidates.empty else pd.Series(dtype=float)
            raw = candidates.pow(alpha) / vol.clip(lower=0.03).pow(beta)
            if raw.empty or raw.sum() <= 0:
                return pd.Series(0.0, index=scores.index)
            return (raw / raw.sum()).reindex(scores.index).fillna(0.0)
        if engine_type == "cluster_budget_then_within_cluster":
            return self._allocate_explicit_groups(params=params, scores=scores, returns_history=returns_history)
        raise ValueError(f"不支持的 AllocationEngine 类型：{engine_type}")

    def _apply_risk_overlays(
        self,
        config: dict[str, Any],
        *,
        date: pd.Timestamp,
        weights: pd.Series,
        prev_weights: pd.Series,
        returns_history: pd.DataFrame,
        policy_config: dict[str, Any],
    ) -> pd.Series:
        result = weights.copy().fillna(0.0)
        for overlay in config.get("overlays", []):
            overlay_type = overlay.get("type")
            params = dict(overlay.get("params") or {})
            if overlay_type == "turnover_cap":
                cap = float(params.get("max_daily_turnover", 0.4))
                delta = result.subtract(prev_weights, fill_value=0.0)
                turnover = float(delta.abs().sum())
                if turnover > cap > 0:
                    result = prev_weights.add(delta * (cap / turnover), fill_value=0.0)
            elif overlay_type == "budget_smoothing":
                smooth = float(params.get("smooth", 0.4))
                result = prev_weights.mul(smooth).add(result.mul(1.0 - smooth), fill_value=0.0)
            elif overlay_type == "cash_buffer":
                buffer = float(params.get("cash_buffer", 0.0))
                result = result * max(0.0, 1.0 - buffer)
            elif overlay_type == "vol_target":
                target_vol = float(params.get("target_vol", 0.18))
                vol_window = int(params.get("vol_window", 60))
                min_gross = float(params.get("min_gross", 0.3))
                max_gross = float(params.get("max_gross", 1.0))
                port_ret = returns_history[result.index].tail(vol_window).fillna(0.0).dot(result)
                vol = float(port_ret.std(ddof=0) * math.sqrt(252)) if len(port_ret) > 2 else 0.0
                if vol > 0:
                    scale = min(max(target_vol / vol, min_gross), max_gross)
                    result = result * scale
            elif overlay_type == "drawdown_cut":
                drawdown_window = int(params.get("drawdown_window", 120))
                limit = float(params.get("portfolio_drawdown_limit", -0.15))
                cut_ratio = float(params.get("cut_ratio", 0.5))
                port_ret = returns_history[result.index].tail(drawdown_window).fillna(0.0).dot(result)
                if len(port_ret) > 2:
                    wealth = (1.0 + port_ret).cumprod()
                    dd = float((wealth / wealth.cummax() - 1.0).min())
                    if dd < limit:
                        result = result * max(0.0, 1.0 - cut_ratio)
            elif overlay_type == "high_vol_discount":
                short = int(params.get("short_vol_window", 20))
                long = int(params.get("long_vol_window", 60))
                strength = float(params.get("discount_strength", 0.4))
                short_vol = returns_history[result.index].tail(short).std(ddof=0)
                long_vol = returns_history[result.index].tail(long).std(ddof=0).replace(0, np.nan)
                ratio = (short_vol / long_vol).replace([np.inf, -np.inf], np.nan).fillna(1.0)
                discount = (1.0 - strength * (ratio - 1.0).clip(lower=0.0)).clip(lower=0.0, upper=1.0)
                result = result * discount
            elif overlay_type == "cluster_cap":
                if not params.get("groups"):
                    allocation_params = dict(policy_config.get("allocation_engine", {}).get("params") or {})
                    if allocation_params.get("cluster_source") == "explicit":
                        params["groups"] = allocation_params.get("groups") or []
                result = self._apply_cluster_cap(result, params)
            else:
                raise ValueError(f"不支持的 RiskOverlay 类型：{overlay_type}")
        return result.reindex(weights.index).fillna(0.0)

    def _should_rebalance(
        self,
        config: dict[str, Any],
        *,
        idx: int,
        date: pd.Timestamp,
        proposed: pd.Series,
        prev_weights: pd.Series,
        last_rebalance_idx: int | None,
        returns_history: pd.DataFrame,
    ) -> bool:
        scheduler_type = config.get("type") or "daily"
        params = dict(config.get("params") or {})
        if idx == 0 or last_rebalance_idx is None:
            return True
        turnover = float(proposed.subtract(prev_weights, fill_value=0.0).abs().sum())
        if scheduler_type == "daily":
            return True
        if scheduler_type == "every_n_days":
            return idx - last_rebalance_idx >= int(params.get("rebalance_days", 5))
        if scheduler_type == "every_n_days_with_threshold":
            if idx - last_rebalance_idx < int(params.get("rebalance_days", 5)):
                return False
            min_weight_change = float(params.get("min_weight_change", 0.05))
            min_total_turnover = float(params.get("min_total_turnover", 0.0))
            max_single_change = float(proposed.subtract(prev_weights, fill_value=0.0).abs().max())
            return max_single_change >= min_weight_change or turnover >= min_total_turnover
        if scheduler_type == "risk_triggered":
            min_days = int(params.get("min_days_between_rebalance", 5))
            if idx - last_rebalance_idx < min_days:
                return False
            vol_trigger = float(params.get("vol_trigger", 0.25))
            drawdown_trigger = float(params.get("drawdown_trigger", -0.10))
            port_ret = returns_history[prev_weights.index].tail(60).fillna(0.0).dot(prev_weights)
            vol = float(port_ret.std(ddof=0) * math.sqrt(252)) if len(port_ret) > 2 else 0.0
            wealth = (1.0 + port_ret).cumprod()
            dd = float((wealth / wealth.cummax() - 1.0).min()) if len(wealth) else 0.0
            return vol >= vol_trigger or dd <= drawdown_trigger
        if scheduler_type == "calendar_and_threshold":
            calendar = str(params.get("calendar") or "weekly")
            min_weight_change = float(params.get("min_weight_change", 0.05))
            previous_date = returns_history.index[-2] if len(returns_history.index) >= 2 else date
            is_calendar = date.weekday() < previous_date.weekday() if calendar == "weekly" else date.month != previous_date.month
            max_single_change = float(proposed.subtract(prev_weights, fill_value=0.0).abs().max())
            return bool(is_calendar and max_single_change >= min_weight_change)
        raise ValueError(f"不支持的 RebalanceScheduler 类型：{scheduler_type}")

    def _project_constraints(
        self,
        config: dict[str, Any],
        weights: pd.Series,
        *,
        unavailable: list[str],
        symbols: list[str],
    ) -> pd.Series:
        projector_type = config.get("type") or "long_only_cap_normalize"
        if projector_type not in {"long_only_cap_normalize", "long_only_full_invest_cap"}:
            raise ValueError(f"不支持的 ConstraintProjector 类型：{projector_type}")
        params = dict(config.get("params") or {})
        gross = float(params.get("gross_exposure", 1.0))
        max_weight = float(params.get("max_asset_weight", 0.25))
        min_weight = float(params.get("min_weight", 0.0))
        max_holding_count = params.get("max_holding_count")
        renormalize = bool(params.get("renormalize_after_clip", True))
        rounding_digits = int(params.get("rounding_digits", 6))
        target = min(max(gross, 0.0), 1.0)
        result = weights.reindex(symbols).fillna(0.0).clip(lower=0.0)
        if unavailable:
            result.loc[[symbol for symbol in unavailable if symbol in result.index]] = 0.0
        if min_weight > 0:
            result[result < min_weight] = 0.0
        if max_holding_count is not None:
            keep = result.sort_values(ascending=False).head(int(max_holding_count)).index
            result = result.where(result.index.isin(keep), 0.0)
        if result.sum() > 0 and renormalize:
            result = result / result.sum() * min(target, max_weight * max(int((result > 0).sum()), 1))
        result = self._cap_and_redistribute(result, max_weight=max_weight, target_gross=target if renormalize else float(result.sum()))
        if min_weight > 0:
            result[result < min_weight] = 0.0
            if result.sum() > 0 and renormalize:
                result = self._cap_and_redistribute(result / result.sum() * min(target, max_weight * max(int((result > 0).sum()), 1)), max_weight=max_weight, target_gross=target)
        result = result.round(rounding_digits)
        if result.sum() > target:
            excess = float(result.sum() - target)
            for symbol in result.sort_values(ascending=False).index:
                if excess <= 0:
                    break
                reduction = min(float(result.loc[symbol]), excess)
                result.loc[symbol] = result.loc[symbol] - reduction
                excess -= reduction
            result = result.clip(lower=0.0).round(rounding_digits)
        result.attrs["constraint_events"] = []
        return result.reindex(symbols).fillna(0.0)

    def _cap_and_redistribute(self, weights: pd.Series, *, max_weight: float, target_gross: float) -> pd.Series:
        result = weights.copy().clip(lower=0.0)
        active = result > 0
        capacity_total = max_weight * int(active.sum())
        target = min(target_gross, capacity_total)
        if result.sum() <= 0 or target <= 0:
            return result * 0.0
        result = result / result.sum() * target
        for _ in range(20):
            over = result > max_weight
            if not over.any():
                break
            excess = float((result[over] - max_weight).sum())
            result[over] = max_weight
            under = (result > 0) & (result < max_weight)
            capacity = max_weight - result[under]
            if excess <= 1e-12 or capacity.sum() <= 0:
                break
            result[under] = result[under] + excess * capacity / capacity.sum()
        return result.clip(upper=max_weight)

    def _allocate_explicit_groups(self, *, params: dict[str, Any], scores: pd.Series, returns_history: pd.DataFrame) -> pd.Series:
        if params.get("cluster_source") != "explicit":
            raise ValueError("cluster_budget_then_within_cluster 只支持 cluster_source=explicit")
        groups = list(params.get("groups") or [])
        if not groups:
            raise ValueError("cluster_budget_then_within_cluster 缺少 groups")
        cluster_cap = float(params.get("cluster_cap", 0.4))
        method = str(params.get("within_cluster_method") or "score_weighted")
        top_k_per_cluster = int(params.get("top_k_per_cluster", 2))
        group_scores: dict[str, float] = {}
        group_symbols: dict[str, list[str]] = {}
        used: set[str] = set()
        for group in groups:
            group_id = str(group.get("group_id") or group.get("group_name") or f"group_{len(group_scores) + 1}")
            symbols = [str(symbol).upper() for symbol in group.get("symbols") or [] if str(symbol).upper() in scores.index]
            symbols = [symbol for symbol in symbols if symbol not in used]
            used.update(symbols)
            if not symbols:
                continue
            selected = scores.loc[symbols].clip(lower=0.0).sort_values(ascending=False).head(top_k_per_cluster)
            selected = selected[selected > 0]
            if selected.empty:
                continue
            group_symbols[group_id] = list(selected.index)
            group_scores[group_id] = float(selected.mean())
        if not group_scores:
            return pd.Series(0.0, index=scores.index)
        group_raw = pd.Series(group_scores).clip(lower=0.0)
        group_budget = group_raw / group_raw.sum()
        group_budget = group_budget.clip(upper=cluster_cap)
        if group_budget.sum() > 0:
            group_budget = group_budget / group_budget.sum()
        result = pd.Series(0.0, index=scores.index, dtype=float)
        for group_id, budget in group_budget.items():
            symbols = group_symbols[group_id]
            group_score = scores.loc[symbols].clip(lower=0.0)
            if method == "equal" or group_score.sum() <= 0:
                inner = pd.Series(1.0 / len(symbols), index=symbols)
            elif method == "inverse_vol":
                vol = returns_history[symbols].tail(60).std(ddof=0).clip(lower=0.03)
                inner = (1.0 / vol) / (1.0 / vol).sum()
            else:
                inner = group_score / group_score.sum()
            result.loc[inner.index] = inner * float(budget)
        return result

    def _apply_cluster_cap(self, weights: pd.Series, params: dict[str, Any]) -> pd.Series:
        if params.get("cluster_source") != "explicit":
            raise ValueError("cluster_cap 只支持 cluster_source=explicit")
        groups = list(params.get("groups") or [])
        cluster_cap = float(params.get("cluster_cap", 0.4))
        if not groups:
            return weights
        result = weights.copy()
        for group in groups:
            symbols = [str(symbol).upper() for symbol in group.get("symbols") or [] if str(symbol).upper() in result.index]
            total = float(result.loc[symbols].sum()) if symbols else 0.0
            if total > cluster_cap > 0:
                result.loc[symbols] = result.loc[symbols] * (cluster_cap / total)
        return result

    def _normalize_scores(self, scores: pd.Series, *, method: str) -> pd.Series:
        values = scores.replace([np.inf, -np.inf], np.nan)
        if values.dropna().empty:
            return pd.Series(0.0, index=scores.index)
        if method == "rank_pct":
            return values.rank(pct=True).fillna(0.0)
        if method == "zscore":
            std = values.std(ddof=0)
            return ((values - values.mean()) / std).fillna(0.0) if std and std > 0 else pd.Series(0.0, index=scores.index)
        if method == "minmax":
            span = values.max() - values.min()
            return ((values - values.min()) / span).fillna(0.0) if span and span > 0 else pd.Series(0.0, index=scores.index)
        if method == "none":
            return values.fillna(0.0)
        raise ValueError(f"不支持的 scorer normalization：{method}")

    def _momentum(self, close_history: pd.DataFrame, window: int) -> pd.Series:
        if len(close_history) <= window:
            return pd.Series(0.0, index=close_history.columns)
        current = close_history.iloc[-1]
        past = close_history.iloc[-window - 1]
        return (current / past - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    def _signal_quality_score(self, record: dict[str, Any], params: dict[str, Any]) -> float:
        metrics = record.get("metrics") or record.get("final_metrics") or {}
        sharpe = float(metrics.get("sharpe") or 0.0)
        max_dd = float(metrics.get("max_drawdown") or 0.0)
        wf = float(metrics.get("walk_forward_score") or metrics.get("walk_forward_mean_score") or 0.0)
        return (
            float(params.get("sharpe_weight", 0.5)) * sharpe
            + float(params.get("drawdown_weight", 0.3)) * (1.0 + max_dd)
            + float(params.get("stability_weight", 0.2)) * wf
        )

    def _validate_policy_config(self, policy_config: dict[str, Any]) -> None:
        required = [
            "universe_gate",
            "asset_scorer",
            "allocation_engine",
            "risk_overlay",
            "rebalance_scheduler",
            "constraint_projector",
            "diagnostics",
        ]
        missing = [key for key in required if key not in policy_config]
        if missing:
            raise ValueError(f"budget_policy_config.json 缺少顶层模块：{missing}")
        text = json.dumps(policy_config, ensure_ascii=False)
        if '"cluster_source": "correlation"' in text or '"cluster_source": "metadata"' in text:
            raise ValueError("预算层第一版只支持 cluster_source=explicit")

    def _build_summary(self, *, weights: pd.DataFrame, turnover: pd.DataFrame, policy_config: dict[str, Any]) -> dict[str, Any]:
        gross = weights.sum(axis=1)
        holding_count = (weights > 0).sum(axis=1)
        max_asset_weight = weights.max(axis=1)
        return {
            "policy_name": policy_config.get("policy_name"),
            "date_count": int(len(weights)),
            "asset_count": int(len(weights.columns)),
            "start_date": str(pd.Timestamp(weights.index.min()).date()) if len(weights) else None,
            "end_date": str(pd.Timestamp(weights.index.max()).date()) if len(weights) else None,
            "average_gross_exposure": float(gross.mean()) if len(gross) else 0.0,
            "max_gross_exposure": float(gross.max()) if len(gross) else 0.0,
            "average_holding_count": float(holding_count.mean()) if len(holding_count) else 0.0,
            "max_holding_count": int(holding_count.max()) if len(holding_count) else 0,
            "max_asset_weight_observed": float(max_asset_weight.max()) if len(max_asset_weight) else 0.0,
            "average_turnover": float(turnover["turnover"].mean()) if not turnover.empty else 0.0,
            "total_turnover": float(turnover["turnover"].sum()) if not turnover.empty else 0.0,
            "rebalance_count": int(turnover["rebalanced"].sum()) if not turnover.empty else 0,
        }

    def _load_panel(self, path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        panel = pd.read_parquet(path)
        panel["datetime"] = pd.to_datetime(panel["datetime"]).dt.normalize()
        panel["symbol"] = normalize_symbol_series(panel["symbol"], reference_symbols=reference_symbols)
        return panel.sort_values(["datetime", "symbol"])

    def _load_returns(self, path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        return load_wide_parquet(path, reference_symbols=reference_symbols)

    def _reference_symbols_from_state(self, state: dict[str, Any]) -> list[str]:
        symbols: list[str] = []
        asset_symbols = state.get("asset_pool", {}).get("symbols")
        if isinstance(asset_symbols, list):
            symbols.extend(str(item).upper() for item in asset_symbols)
        signal_manifest_path = state.get("signal_artifacts", {}).get("manifest_path")
        if signal_manifest_path:
            path = self._resolve_path(signal_manifest_path)
            if path.exists():
                manifest = json.loads(path.read_text(encoding="utf-8-sig"))
                for record in manifest.get("records", []):
                    if record.get("symbol"):
                        symbols.append(str(record["symbol"]).upper())
        return sorted({item for item in symbols if item})

    def _load_metadata(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        path_text = state.get("asset_pool", {}).get("asset_metadata_path")
        if not path_text:
            return {}
        path = self._resolve_path(path_text)
        if not path.exists():
            return {}
        rows = json.loads(path.read_text(encoding="utf-8"))
        return {str(row.get("symbol")).upper(): row for row in rows if row.get("symbol")}

    def _load_signal_records(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        path_text = state.get("signal_artifacts", {}).get("manifest_path")
        if not path_text:
            return {}
        path = self._resolve_path(path_text)
        if not path.exists():
            return {}
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return {str(row.get("symbol")).upper(): row for row in manifest.get("records", []) if row.get("symbol")}

    def _resolve_panel_path(self, state: dict[str, Any], override: Path | None) -> Path:
        if override is not None:
            return self._resolve_path(override)
        path = state.get("data_panel", {}).get("panel_ohlcv")
        if not path:
            raise ValueError("budget_run_state.json 中缺少 data_panel.panel_ohlcv，请先运行 budget data-panel。")
        return self._resolve_path(path)

    def _resolve_returns_path(self, state: dict[str, Any], override: Path | None) -> Path:
        if override is not None:
            return self._resolve_path(override)
        path = state.get("data_panel", {}).get("returns_wide")
        if not path:
            raise ValueError("budget_run_state.json 中缺少 data_panel.returns_wide，请先运行 budget data-panel。")
        return self._resolve_path(path)

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any], policy_id: str) -> Path:
        if output_dir is not None:
            return self._resolve_path(output_dir)
        policies_dir = state.get("directories", {}).get("policies")
        if policies_dir:
            return self._resolve_path(policies_dir) / "executions" / policy_id
        return self.config.root_dir / "artifacts" / "budget_runs" / str(state.get("budget_run_id")) / "policies" / "executions" / policy_id

    def _make_policy_id(self, policy_config: dict[str, Any], policy_config_path: Path) -> str:
        raw = str(policy_config.get("policy_name") or policy_config_path.parent.name or "budget_policy")
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw).strip("_") or "budget_policy"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{safe}_{timestamp}"

    def _update_run_state(self, *, state_path: Path, state: dict[str, Any], policy_id: str, manifest: dict[str, Any], manifest_path: Path) -> None:
        now = datetime.now().isoformat()
        state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("executions", {})[policy_id] = {
            "manifest_path": str(self._relative(manifest_path)),
            "daily_budget_weights": manifest["daily_budget_weights_path"],
            "daily_scores": manifest["daily_scores_path"],
            "gate_results": manifest["gate_results_path"],
            "turnover": manifest["turnover_path"],
            "summary": manifest["summary"],
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetPolicyEngine",
                "event": "budget_policy_execution_completed",
                "summary": f"预算策略配置已执行：{policy_id}",
                "policy_id": policy_id,
                "manifest_path": str(self._relative(manifest_path)),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(path)
