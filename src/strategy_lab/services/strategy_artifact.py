from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.signals.base import SignalStrategyProtocol
from strategy_lab.services.signal_run import SignalRunManager


class StrategyArtifactRequest(BaseModel):
    run_state_path: Path
    attempt_id: str
    strategy_path: Path | None = None
    strategy_spec_path: Path | None = None
    param_space_path: Path | None = None
    strategy_meta_path: Path | None = None
    template: str | None = None
    strategy_name: str | None = None
    strategy_class_name: str = "Strategy"
    param_space: dict[str, Any] | None = None
    strategy_meta: dict[str, Any] | None = None
    strategy_spec: str | None = None


class StrategyArtifactResult(BaseModel):
    run_state_path: Path
    attempt_id: str
    strategy_ref: str
    strategy_path: Path
    strategy_spec_path: Path
    param_space_path: Path
    strategy_meta_path: Path
    summary: dict[str, Any] = Field(default_factory=dict)


class StrategyArtifactService:
    """策略产物落盘与校验服务。

    SignalAgent 可以自主生成策略脚本；本服务只负责把脚本、说明和参数空间保存到 attempt 标准目录。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)

    def run(self, request: StrategyArtifactRequest) -> StrategyArtifactResult:
        state = self.run_manager.load_state(request.run_state_path)
        attempt = self._find_attempt(state, request.attempt_id)
        strategy_dir = self._resolve_path(attempt["strategy_dir"])
        strategy_dir.mkdir(parents=True, exist_ok=True)

        if request.template:
            payload = self._template_payload(request.template)
            source_strategy_path = None
            strategy_code = payload["strategy_code"]
            spec_text = request.strategy_spec or payload["strategy_spec"]
            param_space = request.param_space or payload["param_space"]
            meta = {**payload["strategy_meta"], **(request.strategy_meta or {})}
        else:
            if request.strategy_path is None:
                raise ValueError("未指定 template 时必须传 strategy_path。")
            source_strategy_path = self._resolve_path(request.strategy_path)
            strategy_code = None
            spec_text = request.strategy_spec
            param_space = request.param_space
            meta = request.strategy_meta or {}

        strategy_path = strategy_dir / "strategy.py"
        strategy_spec_path = strategy_dir / "strategy_spec.md"
        param_space_path = strategy_dir / "param_space.json"
        strategy_meta_path = strategy_dir / "strategy_meta.json"

        if source_strategy_path:
            if not source_strategy_path.exists():
                raise FileNotFoundError(f"策略脚本不存在：{source_strategy_path}")
            shutil.copy2(source_strategy_path, strategy_path)
        elif strategy_code is not None:
            strategy_path.write_text(strategy_code, encoding="utf-8")

        if request.strategy_spec_path:
            source_spec_path = self._resolve_path(request.strategy_spec_path)
            shutil.copy2(source_spec_path, strategy_spec_path)
        else:
            strategy_spec_path.write_text(spec_text or "# 策略说明\n\n未提供策略说明。\n", encoding="utf-8")

        if request.param_space_path:
            source_param_path = self._resolve_path(request.param_space_path)
            shutil.copy2(source_param_path, param_space_path)
        else:
            param_space_path.write_text(
                json.dumps(param_space or {}, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

        if request.strategy_meta_path:
            source_meta_path = self._resolve_path(request.strategy_meta_path)
            shutil.copy2(source_meta_path, strategy_meta_path)
        else:
            meta = {
                "strategy_name": request.strategy_name or meta.get("strategy_name") or strategy_path.stem,
                "strategy_class_name": request.strategy_class_name,
                "created_at": datetime.now().isoformat(),
                **meta,
            }
            strategy_meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        strategy_ref = f"{self._relative(strategy_path)}:{request.strategy_class_name}"
        self._validate_strategy(strategy_path=strategy_path, class_name=request.strategy_class_name)
        self.run_manager.update_attempt(
            request.run_state_path,
            attempt_id=request.attempt_id,
            status="strategy_saved",
            fields={
                "strategy_ref": strategy_ref,
                "strategy_path": str(self._relative(strategy_path)),
                "strategy_spec_path": str(self._relative(strategy_spec_path)),
                "param_space_path": str(self._relative(param_space_path)),
                "strategy_meta_path": str(self._relative(strategy_meta_path)),
                "strategy_name": request.strategy_name or self._read_strategy_name(strategy_meta_path),
            },
        )
        self.run_manager.append_event(
            request.run_state_path,
            actor="StrategyArtifactService",
            event="strategy_artifact_saved",
            summary=f"保存并校验策略产物：{request.attempt_id}",
            extra={"attempt_id": request.attempt_id, "strategy_ref": strategy_ref},
        )

        return StrategyArtifactResult(
            run_state_path=self._resolve_path(request.run_state_path),
            attempt_id=request.attempt_id,
            strategy_ref=strategy_ref,
            strategy_path=strategy_path,
            strategy_spec_path=strategy_spec_path,
            param_space_path=param_space_path,
            strategy_meta_path=strategy_meta_path,
            summary={
                "strategy_name": request.strategy_name or self._read_strategy_name(strategy_meta_path),
                "validated": True,
            },
        )

    def _template_payload(self, template: str) -> dict[str, Any]:
        key = template.strip().lower()
        if key not in {"ma_crossover", "ma20", "moving_average"}:
            raise ValueError(f"未知策略模板：{template}")
        return {
            "strategy_code": self._ma_crossover_code(),
            "strategy_spec": self._ma_crossover_spec(),
            "param_space": {
                "ma_window": {"type": "int", "values": [5, 10, 20, 30, 60, 90, 120], "default": 20},
                "band": {"type": "float", "values": [0.0, 0.005, 0.01, 0.02, 0.03], "default": 0.0},
                "warmup_signal": {"type": "float", "values": [0.0], "default": 0.0},
                "long_signal": {"type": "float", "values": [1.0], "default": 1.0},
                "cash_signal": {"type": "float", "values": [0.0], "default": 0.0},
            },
            "strategy_meta": {
                "strategy_name": "ma_crossover_baseline",
                "strategy_family": "trend_following",
                "template": key,
            },
        }

    def _ma_crossover_code(self) -> str:
        return '''from __future__ import annotations

from typing import Any

import pandas as pd

from strategy_lab.signals.base import BaseSignalStrategy


class Strategy(BaseSignalStrategy):
    """均线趋势信号策略。

    收盘价高于 N 日均线加 band 阈值时输出 long_signal，否则输出 cash_signal。
    """

    SIGNAL_META: dict[str, Any] = {
        "name": "ma_crossover_baseline",
        "version": "0.1.0",
        "description": "Close above moving average threshold => long_signal, otherwise cash_signal.",
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
        close = pd.to_numeric(history["close"], errors="coerce").dropna()
        if len(close) < window:
            return self._clamp(warmup_signal)
        ma = float(close.iloc[-window:].mean())
        latest_close = float(close.iloc[-1])
        if latest_close > ma * (1.0 + band):
            return self._clamp(long_signal)
        return self._clamp(cash_signal)
'''

    def _ma_crossover_spec(self) -> str:
        return """# MA Crossover Baseline

## 策略逻辑

当最新收盘价高于指定窗口均线并超过 band 阈值时，目标仓位为 long_signal；否则目标仓位为 cash_signal。样本不足 ma_window 时使用 warmup_signal。

## 参数

- ma_window：均线窗口。
- band：均线阈值，降低贴线频繁切换。
- warmup_signal：样本不足时的仓位信号。
- long_signal：看多状态下的仓位信号。
- cash_signal：非看多状态下的仓位信号。
"""

    def _validate_strategy(self, *, strategy_path: Path, class_name: str) -> None:
        spec = importlib.util.spec_from_file_location(f"strategy_artifact_{strategy_path.stem}", strategy_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载策略脚本：{strategy_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        strategy_cls = getattr(module, class_name)
        instance = strategy_cls({})
        if not isinstance(instance, SignalStrategyProtocol):
            raise TypeError("策略类必须实现 suggest(history, current_position_in_budget) 方法。")

    def _find_attempt(self, state: dict[str, Any], attempt_id: str) -> dict[str, Any]:
        attempt = next((item for item in state.get("attempts", []) if item.get("attempt_id") == attempt_id), None)
        if attempt is None:
            raise ValueError(f"run_state.json 中不存在 attempt：{attempt_id}")
        return attempt

    def _read_strategy_name(self, strategy_meta_path: Path) -> str | None:
        try:
            return json.loads(strategy_meta_path.read_text(encoding="utf-8")).get("strategy_name")
        except Exception:
            return None

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.config.root_dir.resolve())
        except ValueError:
            return path
