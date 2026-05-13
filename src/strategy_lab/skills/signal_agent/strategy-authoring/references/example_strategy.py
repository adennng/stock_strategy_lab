from __future__ import annotations

import math

import pandas as pd

from strategy_lab.signals.base import BaseSignalStrategy


class Strategy(BaseSignalStrategy):
    """示例策略：显式 RegimeAlphaMap + 多周期 Alpha。

    这是给 SignalAgent 学习结构的模板，不是要求照抄的最终策略。
    关键点：每个核心 regime 必须调用不同 Alpha 函数，然后统一进入仓位映射。
    """

    REGIME_ALPHA_MAP = {
        "uptrend": "alpha_uptrend_trend_momentum",
        "range": "alpha_range_mean_reversion",
        "downtrend": "alpha_downtrend_flat",
        "high_vol": "alpha_high_vol_defensive",
    }

    SIGNAL_META = {
        "name": "example_regime_alpha_map_signal",
        "version": "0.3.0",
        "description": "Explicit RegimeAlphaMap with separate alpha functions for each regime.",
        "regime_alpha_map": REGIME_ALPHA_MAP,
    }

    def suggest(self, history: pd.DataFrame, current_position_in_budget: float = 0.0) -> float:
        long_window = int(self.params.get("long_window", 120))
        mid_window = int(self.params.get("mid_window", 40))
        short_window = int(self.params.get("short_window", 10))
        vol_window = int(self.params.get("vol_window", 20))
        trend_threshold = float(self.params.get("trend_threshold", 0.02))
        range_z_entry = float(self.params.get("range_z_entry", -1.0))
        high_vol_threshold = float(self.params.get("high_vol_threshold", 0.03))
        uptrend_max_s = float(self.params.get("uptrend_max_s", 1.0))
        range_max_s = float(self.params.get("range_max_s", 0.45))
        downtrend_max_s = float(self.params.get("downtrend_max_s", 0.0))
        high_vol_max_s = float(self.params.get("high_vol_max_s", 0.15))
        max_daily_change = float(self.params.get("max_daily_target_change", 0.35))

        close = self._close(history)
        required = max(long_window, mid_window, short_window, vol_window) + 2
        if len(close) < required:
            return self._clamp(current_position_in_budget)

        regime = self.detect_regime(
            close=close,
            long_window=long_window,
            mid_window=mid_window,
            vol_window=vol_window,
            trend_threshold=trend_threshold,
            high_vol_threshold=high_vol_threshold,
        )

        if regime == "uptrend":
            alpha_score = self.alpha_uptrend_trend_momentum(close, mid_window=mid_window, short_window=short_window)
        elif regime == "range":
            alpha_score = self.alpha_range_mean_reversion(close, mid_window=mid_window, short_window=short_window, z_entry=range_z_entry)
        elif regime == "high_vol":
            alpha_score = self.alpha_high_vol_defensive(close, vol_window=vol_window)
        else:
            alpha_score = self.alpha_downtrend_flat(close)

        target_s = self.map_position(
            alpha_score=alpha_score,
            regime=regime,
            uptrend_max_s=uptrend_max_s,
            range_max_s=range_max_s,
            downtrend_max_s=downtrend_max_s,
            high_vol_max_s=high_vol_max_s,
        )
        target_s = self.apply_state_rules(
            target_s=target_s,
            current_position_in_budget=current_position_in_budget,
            max_daily_change=max_daily_change,
        )
        return self._clamp(target_s)

    def detect_regime(
        self,
        close: pd.Series,
        long_window: int,
        mid_window: int,
        vol_window: int,
        trend_threshold: float,
        high_vol_threshold: float,
    ) -> str:
        long_ma = close.rolling(long_window).mean()
        mid_ma = close.rolling(mid_window).mean()
        long_slope = long_ma.diff(max(5, mid_window // 2)).iloc[-1] / max(abs(long_ma.iloc[-1]), 1e-12)
        realized_vol = close.pct_change().rolling(vol_window).std().iloc[-1]

        if pd.notna(realized_vol) and float(realized_vol) >= high_vol_threshold:
            return "high_vol"
        if close.iloc[-1] > long_ma.iloc[-1] and mid_ma.iloc[-1] > long_ma.iloc[-1] and long_slope > 0:
            return "uptrend"
        mid_return = close.iloc[-1] / close.iloc[-mid_window] - 1.0
        if abs(float(mid_return)) <= trend_threshold:
            return "range"
        return "downtrend"

    def alpha_uptrend_trend_momentum(self, close: pd.Series, mid_window: int, short_window: int) -> float:
        mid_momentum = close.iloc[-1] / close.iloc[-mid_window] - 1.0
        short_momentum = close.iloc[-1] / close.iloc[-short_window] - 1.0
        short_ma = close.rolling(short_window).mean().iloc[-1]
        trend_score = self._linear_score(mid_momentum, low=0.0, high=0.12)
        confirm_score = 1.0 if close.iloc[-1] > short_ma and short_momentum > -0.03 else 0.5
        return self._clamp(0.75 * trend_score + 0.25 * confirm_score)

    def alpha_range_mean_reversion(self, close: pd.Series, mid_window: int, short_window: int, z_entry: float) -> float:
        mid_ma = close.rolling(mid_window).mean().iloc[-1]
        mid_std = close.rolling(mid_window).std().iloc[-1]
        if pd.isna(mid_std) or float(mid_std) <= 1e-12:
            return 0.0
        z_score = (close.iloc[-1] - mid_ma) / mid_std
        short_return = close.iloc[-1] / close.iloc[-short_window] - 1.0
        if z_score <= z_entry and short_return < 0:
            return self._clamp(min(1.0, abs(float(z_score)) / max(abs(z_entry), 1e-12)))
        return 0.0

    def alpha_downtrend_flat(self, close: pd.Series) -> float:
        return 0.0

    def alpha_high_vol_defensive(self, close: pd.Series, vol_window: int) -> float:
        recent_return = close.iloc[-1] / close.iloc[-min(len(close), vol_window)] - 1.0
        return 0.25 if recent_return > 0 else 0.0

    def map_position(
        self,
        alpha_score: float,
        regime: str,
        uptrend_max_s: float,
        range_max_s: float,
        downtrend_max_s: float,
        high_vol_max_s: float,
    ) -> float:
        max_by_regime = {
            "uptrend": uptrend_max_s,
            "range": range_max_s,
            "downtrend": downtrend_max_s,
            "high_vol": high_vol_max_s,
        }
        return self._clamp(alpha_score * float(max_by_regime.get(regime, 0.0)))

    def apply_state_rules(self, target_s: float, current_position_in_budget: float, max_daily_change: float) -> float:
        delta = target_s - float(current_position_in_budget)
        if abs(delta) > max_daily_change:
            target_s = float(current_position_in_budget) + math.copysign(max_daily_change, delta)
        return self._clamp(target_s)

    def _close(self, history: pd.DataFrame) -> pd.Series:
        if history.empty or "close" not in history:
            return pd.Series(dtype=float)
        return pd.to_numeric(history["close"], errors="coerce").dropna()

    def _linear_score(self, value: float, low: float, high: float) -> float:
        if high <= low:
            return 0.0
        return self._clamp((float(value) - low) / (high - low))
