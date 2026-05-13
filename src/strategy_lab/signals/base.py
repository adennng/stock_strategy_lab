from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

import pandas as pd


def clamp_signal(value: float, allow_short: bool = False) -> float:
    """把信号限制在合法区间内。信号层默认只允许 0 到 1 的预算使用比例。"""
    number = float(value)
    lower = -1.0 if allow_short else 0.0
    return max(lower, min(1.0, number))


@runtime_checkable
class SignalStrategyProtocol(Protocol):
    """信号层策略协议。

    策略每根 K 线输出一个目标预算使用比例 S：
    - S=0 表示空仓；
    - S=1 表示用满该资产预算；
    - 当前阶段默认不允许做空，因此 S 必须在 0 到 1 之间。
    """

    params: dict[str, Any]

    def suggest(self, history: pd.DataFrame, current_position_in_budget: float = 0.0) -> float:
        """根据截至当前 K 线的历史数据输出目标信号 S。"""


class BaseSignalStrategy:
    """LLM 生成信号策略时建议继承的基类。"""

    SIGNAL_META: dict[str, Any] = {
        "name": "base_signal_strategy",
        "version": "0.1.0",
        "description": "Base class for signal-layer strategies.",
    }
    PARAM_SPACE: dict[str, Any] = {}

    def __init__(self, params: Mapping[str, Any] | None = None):
        self.params = dict(params or {})

    def suggest(self, history: pd.DataFrame, current_position_in_budget: float = 0.0) -> float:
        raise NotImplementedError

    def _clamp(self, value: float, allow_short: bool = False) -> float:
        return clamp_signal(value, allow_short=allow_short)
