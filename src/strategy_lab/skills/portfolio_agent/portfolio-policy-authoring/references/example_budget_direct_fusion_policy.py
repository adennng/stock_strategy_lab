from __future__ import annotations

from typing import Any

import pandas as pd


class PortfolioFusionPolicy:
    """预算层直用基线示例。

    第一轮建议先用该示例建立预算层自身组合表现基线：直接使用
    budget_weights 作为目标仓位，只施加 max_gross、max_weight 和换手约束。
    """

    def __init__(self, params: dict[str, Any] | None = None):
        defaults = {
            "max_gross": 1.0,
            "max_weight": 0.30,
            "rebalance_speed": 1.0,
            "max_turnover_per_day": 0.60,
        }
        self.params = {**defaults, **(params or {})}

    def generate_weights(
        self,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        returns: pd.DataFrame,
        signal_profile: dict[str, Any] | None = None,
        market_context: dict[str, Any] | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        budget, signal, returns = self._align_inputs(budget_weights, signal_targets, returns)
        raw = budget.clip(lower=0.0, upper=float(self.params["max_weight"]))
        raw = self._scale_to_gross(raw, float(self.params["max_gross"]))
        weights = self._smooth_and_limit_turnover(raw)
        diagnostics = self._build_diagnostics(weights=weights, raw_weights=raw, budget=budget, signal=signal)
        return weights, diagnostics

    def _align_inputs(
        self,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        returns: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        index = budget_weights.index.intersection(signal_targets.index).intersection(returns.index)
        columns = sorted(set(budget_weights.columns) & set(signal_targets.columns) & set(returns.columns))
        if len(index) == 0 or not columns:
            raise ValueError("budget_weights、signal_targets 和 returns 没有可对齐的日期或资产。")
        budget = budget_weights.loc[index, columns].fillna(0.0).clip(lower=0.0)
        signal = signal_targets.loc[index, columns].fillna(0.0).clip(lower=0.0, upper=1.0)
        aligned_returns = returns.loc[index, columns].fillna(0.0)
        return budget, signal, aligned_returns

    def _smooth_and_limit_turnover(self, target: pd.DataFrame) -> pd.DataFrame:
        speed = float(self.params["rebalance_speed"])
        max_turnover = float(self.params["max_turnover_per_day"])
        rows = []
        previous = pd.Series(0.0, index=target.columns)
        for dt, desired in target.iterrows():
            desired = previous + speed * (desired - previous)
            delta = desired - previous
            turnover = float(delta.abs().sum())
            if turnover > max_turnover and turnover > 0:
                desired = previous + delta * (max_turnover / turnover)
            rows.append(desired.rename(dt))
            previous = desired
        return pd.DataFrame(rows).clip(lower=0.0)

    def _build_diagnostics(
        self,
        *,
        weights: pd.DataFrame,
        raw_weights: pd.DataFrame,
        budget: pd.DataFrame,
        signal: pd.DataFrame,
    ) -> pd.DataFrame:
        diagnostics = pd.DataFrame(index=weights.index)
        diagnostics["gross_exposure"] = weights.sum(axis=1)
        diagnostics["cash_weight"] = 1.0 - diagnostics["gross_exposure"]
        diagnostics["turnover"] = weights.diff().abs().sum(axis=1).fillna(weights.abs().sum(axis=1))
        diagnostics["budget_gross"] = budget.sum(axis=1)
        diagnostics["signal_mean"] = signal.mean(axis=1)
        diagnostics["signal_breadth"] = signal.gt(0.3).mean(axis=1)
        diagnostics["raw_gross"] = raw_weights.sum(axis=1)
        diagnostics["over_budget_total"] = (weights - budget).clip(lower=0.0).sum(axis=1)
        diagnostics["budget_signal_rank_corr"] = self._daily_rank_corr(budget, signal)
        return diagnostics

    @staticmethod
    def _daily_rank_corr(budget: pd.DataFrame, signal: pd.DataFrame) -> list[float | None]:
        values = []
        budget_ranks = budget.rank(axis=1, ascending=False)
        signal_ranks = signal.rank(axis=1, ascending=False)
        for dt in budget.index:
            corr = budget_ranks.loc[dt].corr(signal_ranks.loc[dt])
            values.append(None if pd.isna(corr) else float(corr))
        return values

    @staticmethod
    def _scale_to_gross(frame: pd.DataFrame, max_gross: float) -> pd.DataFrame:
        gross = frame.sum(axis=1)
        scale = (max_gross / gross.replace(0.0, pd.NA)).clip(upper=1.0).fillna(1.0)
        return frame.mul(scale, axis=0)
