from __future__ import annotations

from typing import Any

import pandas as pd

from strategy_lab.signals.base import BaseSignalStrategy


class MA20SignalStrategy(BaseSignalStrategy):
    """MA20 基线信号策略。

    规则：收盘价高于 N 日均线时用满预算，低于或等于均线时空仓。
    """

    SIGNAL_META: dict[str, Any] = {
        "name": "ma20_signal",
        "version": "0.1.0",
        "description": "Close above moving average => S=1, otherwise S=0.",
    }
    PARAM_SPACE: dict[str, Any] = {
        "ma_window": {"type": "int", "low": 5, "high": 120, "default": 20},
        "band": {"type": "float", "low": 0.0, "high": 0.05, "default": 0.0},
        "warmup_signal": {"type": "float", "low": 0.0, "high": 1.0, "default": 0.0},
        "long_signal": {"type": "float", "low": 0.0, "high": 1.0, "default": 1.0},
        "cash_signal": {"type": "float", "low": 0.0, "high": 1.0, "default": 0.0},
    }

    def suggest(self, history: pd.DataFrame, current_position_in_budget: float = 0.0) -> float:
        window = int(self.params.get("ma_window", 20))
        band = float(self.params.get("band", 0.0))
        warmup_signal = float(self.params.get("warmup_signal", 0.0))
        long_signal = float(self.params.get("long_signal", 1.0))
        cash_signal = float(self.params.get("cash_signal", 0.0))

        if history.empty or "close" not in history:
            return self._clamp(0.0)
        if len(history) < window:
            return self._clamp(warmup_signal)

        close = pd.to_numeric(history["close"], errors="coerce").dropna()
        if len(close) < window:
            return self._clamp(warmup_signal)

        latest_close = float(close.iloc[-1])
        moving_average = float(close.iloc[-window:].mean())
        threshold = moving_average * (1.0 + band)
        if latest_close > threshold:
            return self._clamp(long_signal)
        return self._clamp(cash_signal)


Strategy = MA20SignalStrategy
