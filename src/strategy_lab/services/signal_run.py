from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.config.loader import load_config_file


class SignalRunPaths(BaseModel):
    run_id: str
    root_dir: Path
    run_state_path: Path
    data_dir: Path
    market_profile_dir: Path
    attempts_dir: Path
    strategies_dir: Path
    reports_dir: Path
    logs_dir: Path


class SignalAttemptPaths(BaseModel):
    run_id: str
    attempt_id: str
    attempt_dir: Path
    strategy_dir: Path
    optimization_dir: Path
    backtests_dir: Path
    full_backtest_dir: Path
    train_backtest_dir: Path
    validation_backtest_dir: Path
    walk_forward_dir: Path
    review_dir: Path
    logs_dir: Path


class SignalRunManager:
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _state_locks: ClassVar[dict[str, threading.RLock]] = {}

    """信号层任务运行状态管理器。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()

    def create_run(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        asset_type: str = "index",
        frequency: str = "1d",
        task_description: str | None = None,
        task_name: str | None = None,
        run_id: str | None = None,
        backtest_overrides: dict[str, Any] | None = None,
        strategy_max_iterations: int | None = None,
    ) -> SignalRunPaths:
        actual_run_id = run_id or self.new_run_id(symbol=symbol)
        paths = self._build_paths(actual_run_id)
        for directory in [
            paths.root_dir,
            paths.data_dir,
            paths.market_profile_dir,
            paths.attempts_dir,
            paths.strategies_dir,
            paths.reports_dir,
            paths.logs_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

        state = self.build_initial_state(
            paths=paths,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            asset_type=asset_type,
            frequency=frequency,
            task_description=task_description,
            task_name=task_name,
            backtest_overrides=backtest_overrides,
            strategy_max_iterations=strategy_max_iterations,
        )
        self.save_state(paths.run_state_path, state)
        return paths

    def build_initial_state(
        self,
        *,
        paths: SignalRunPaths,
        symbol: str,
        start_date: str,
        end_date: str,
        asset_type: str,
        frequency: str,
        task_description: str | None,
        task_name: str | None,
        backtest_overrides: dict[str, Any] | None,
        strategy_max_iterations: int | None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        backtest_config = self._load_backtest_config(backtest_overrides=backtest_overrides)
        description = task_description or f"为 {symbol} 探索单资产信号策略"
        state = {
            "schema_version": "0.2.0",
            "run_id": paths.run_id,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "task": {
                "task_name": task_name or paths.run_id,
                "description": description,
                "asset": {
                    "symbol": symbol,
                    "asset_type": asset_type,
                },
                "data_range": {
                    "start": start_date,
                    "end": end_date,
                    "frequency": frequency,
                },
                "objective": {
                    "primary": "risk_adjusted_return",
                    "constraints": [
                        "信号 S 必须在 0 到 1 之间",
                        "不得使用未来数据",
                        "优先控制最大回撤",
                    ],
                },
            },
            "directories": self._relative_directories(paths),
            "backtest_config": backtest_config,
            "steps": {
                "data_acquisition": {
                    "status": "pending",
                    "agent": "DataAgent",
                    "started_at": None,
                    "finished_at": None,
                    "request": None,
                    "data_source": None,
                    "primary_dataset": None,
                    "dataset_manifest": None,
                    "summary": None,
                    "error": None,
                },
                "market_profile": {
                    "status": "pending",
                    "profile_path": None,
                    "profile_md_path": None,
                    "chart_path": None,
                    "summary": None,
                    "error": None,
                },
                "strategy_search": {
                    "status": "pending",
                    "current_attempt": None,
                    "attempt_count": 0,
                    "best_attempt_id": None,
                    "best_score": None,
                    "max_iterations": self._resolve_strategy_max_iterations(strategy_max_iterations),
                },
                "final_selection": {
                    "status": "pending",
                    "selected_attempt_id": None,
                    "selected_strategy_path": None,
                    "selected_metrics_path": None,
                    "selected_reason": None,
                },
            },
            "attempts": [],
            "artifacts": {
                "datasets": {},
                "market_profile": {},
                "accepted_strategies": {},
                "run_reports": {},
            },
            "events": [
                {
                    "timestamp": now,
                    "actor": "SignalRunManager",
                    "event": "signal_run_created",
                    "summary": f"创建信号层任务 {paths.run_id}",
                }
            ],
        }
        return state

    @staticmethod
    def _resolve_strategy_max_iterations(strategy_max_iterations: int | None) -> int:
        if strategy_max_iterations is not None:
            return int(strategy_max_iterations)
        agent_cfg = load_config_file("agent")
        value = (
            agent_cfg.get("agents", {})
            .get("strategy_agent", {})
            .get("max_strategy_iterations", 20)
        )
        return int(value or 20)

    def new_run_id(self, symbol: str) -> str:
        timestamp = datetime.now(self._timezone()).strftime("%Y%m%d_%H%M%S")
        return f"signal_{self._safe_name(symbol)}_{timestamp}"

    def load_state(self, run_state_path: str | Path) -> dict[str, Any]:
        path = self._resolve_path(run_state_path)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state(self, run_state_path: str | Path, state: dict[str, Any]) -> None:
        path = self._resolve_path(run_state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def append_event(
        self,
        run_state_path: str | Path,
        *,
        actor: str,
        event: str,
        summary: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        payload = {
            "timestamp": self._now_iso(),
            "actor": actor,
            "event": event,
            "summary": summary,
            **(extra or {}),
        }
        state.setdefault("events", []).append(payload)
        state["updated_at"] = payload["timestamp"]
        self.save_state(run_state_path, state)
        return state

    def update_data_acquisition(
        self,
        run_state_path: str | Path,
        *,
        status: str,
        primary_dataset: str | None = None,
        dataset_manifest: str | None = None,
        data_source: str | None = None,
        summary: str | None = None,
        error: str | None = None,
        started_at: str | None = None,
        row_count: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        step = state.setdefault("steps", {}).setdefault("data_acquisition", {})
        now = self._now_iso()
        step.update(
            {
                "status": status,
                "started_at": started_at or step.get("started_at") or now,
                "finished_at": now,
                "primary_dataset": primary_dataset,
                "dataset_manifest": dataset_manifest,
                "data_source": data_source,
                "summary": summary,
                "error": error,
            }
        )
        if primary_dataset or dataset_manifest:
            primary = {
                "file": primary_dataset,
                "manifest": dataset_manifest,
                "source": data_source,
                "row_count": row_count,
                "start_date": start_date,
                "end_date": end_date,
                "columns": columns or [],
            }
            state.setdefault("artifacts", {}).setdefault("datasets", {})["primary"] = {
                key: value for key, value in primary.items() if value is not None and value != []
            }
        event_name = "data_acquisition_completed"
        if status == "failed":
            event_name = "data_acquisition_failed"
        elif status == "partial":
            event_name = "data_acquisition_partial"
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "DataAgent",
                "event": event_name,
                "summary": summary or error or f"数据获取状态更新为 {status}",
                "primary_dataset": primary_dataset,
                "dataset_manifest": dataset_manifest,
                "data_source": data_source,
            }
        )
        state["updated_at"] = now
        self.save_state(run_state_path, state)
        return state

    def append_strategy_attempt(
        self,
        run_state_path: str | Path,
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        attempts = state.setdefault("attempts", [])
        attempts.append(attempt)
        search = state.setdefault("steps", {}).setdefault("strategy_search", {})
        search["attempt_count"] = len(attempts)
        search["current_attempt"] = attempt.get("attempt_id")
        search["status"] = "running"
        state["updated_at"] = self._now_iso()
        self.save_state(run_state_path, state)
        return state

    def new_attempt_id(self, run_state_path: str | Path) -> str:
        state = self.load_state(run_state_path)
        existing = {
            str(item.get("attempt_id"))
            for item in state.get("attempts", [])
            if item.get("attempt_id")
        }
        index = len(existing) + 1
        while True:
            attempt_id = f"attempt_{index:03d}"
            if attempt_id not in existing:
                return attempt_id
            index += 1

    def create_attempt(
        self,
        run_state_path: str | Path,
        *,
        attempt_id: str | None = None,
        status: str = "created",
    ) -> SignalAttemptPaths:
        state = self.load_state(run_state_path)
        actual_attempt_id = attempt_id or self.new_attempt_id(run_state_path)
        paths = self._build_attempt_paths(state=state, attempt_id=actual_attempt_id)
        for directory in [
            paths.attempt_dir,
            paths.strategy_dir,
            paths.optimization_dir,
            paths.backtests_dir,
            paths.full_backtest_dir,
            paths.train_backtest_dir,
            paths.validation_backtest_dir,
            paths.walk_forward_dir,
            paths.review_dir,
            paths.logs_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

        now = self._now_iso()
        attempt_record = {
            "attempt_id": actual_attempt_id,
            "status": status,
            "strategy_dir": self._relative(paths.strategy_dir),
            "optimization_dir": self._relative(paths.optimization_dir),
            "backtests_dir": self._relative(paths.backtests_dir),
            "full_backtest_dir": self._relative(paths.full_backtest_dir),
            "train_backtest_dir": self._relative(paths.train_backtest_dir),
            "validation_backtest_dir": self._relative(paths.validation_backtest_dir),
            "walk_forward_dir": self._relative(paths.walk_forward_dir),
            "review_dir": self._relative(paths.review_dir),
            "logs_dir": self._relative(paths.logs_dir),
            "score": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        self._upsert_attempt_record(state, attempt_record)
        search = state.setdefault("steps", {}).setdefault("strategy_search", {})
        search["status"] = "running"
        search["current_attempt"] = actual_attempt_id
        search["attempt_count"] = len(state.get("attempts", []))
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "SignalRunManager",
                "event": "signal_attempt_created",
                "summary": f"创建策略探索轮次 {actual_attempt_id}",
                "attempt_id": actual_attempt_id,
            }
        )
        state["status"] = "running"
        state["updated_at"] = now
        self.save_state(run_state_path, state)
        return paths

    def update_attempt(
        self,
        run_state_path: str | Path,
        *,
        attempt_id: str,
        status: str | None = None,
        score: float | None = None,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        attempts = state.setdefault("attempts", [])
        attempt = next((item for item in attempts if item.get("attempt_id") == attempt_id), None)
        if attempt is None:
            raise ValueError(f"run_state.json 中不存在 attempt：{attempt_id}")
        now = self._now_iso()
        if status is not None:
            attempt["status"] = status
            if status in {"success", "reviewed", "failed"}:
                attempt["finished_at"] = now
        if score is not None:
            attempt["score"] = score
        if fields:
            attempt.update(fields)
        attempt["updated_at"] = now
        state["updated_at"] = now
        self.save_state(run_state_path, state)
        return state

    def update_strategy_search(
        self,
        run_state_path: str | Path,
        *,
        status: str | None = None,
        current_attempt: str | None = None,
        best_attempt_id: str | None = None,
        best_score: float | None = None,
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        search = state.setdefault("steps", {}).setdefault("strategy_search", {})
        if status is not None:
            search["status"] = status
        if current_attempt is not None:
            search["current_attempt"] = current_attempt
        if best_attempt_id is not None:
            search["best_attempt_id"] = best_attempt_id
        if best_score is not None:
            search["best_score"] = best_score
        search["attempt_count"] = len(state.get("attempts", []))
        state["updated_at"] = self._now_iso()
        self.save_state(run_state_path, state)
        return state

    def update_final_selection(
        self,
        run_state_path: str | Path,
        *,
        best_attempt_id: str,
        best_strategy_path: str,
        best_metrics_path: str,
        reason: str,
        best_score: float | None = None,
        status: str = "success",
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        now = self._now_iso()
        attempts = state.setdefault("attempts", [])
        selected_attempt = next((item for item in attempts if item.get("attempt_id") == best_attempt_id), None)
        if selected_attempt is not None and best_score is None:
            raw_score = selected_attempt.get("score")
            if raw_score is not None:
                try:
                    best_score = float(raw_score)
                except (TypeError, ValueError):
                    best_score = None
        final_selection = state.setdefault("steps", {}).setdefault("final_selection", {})
        final_selection.update(
            {
                "status": status,
                "selected_attempt_id": best_attempt_id,
                "selected_strategy_path": best_strategy_path,
                "selected_metrics_path": best_metrics_path,
                "selected_reason": reason,
            }
        )
        search = state.setdefault("steps", {}).setdefault("strategy_search", {})
        if status == "success":
            search["status"] = "completed"
        search["best_attempt_id"] = best_attempt_id
        if best_score is not None:
            search["best_score"] = best_score
        search["attempt_count"] = len(attempts)
        state["status"] = "completed" if status == "success" else state.get("status", "running")
        artifacts = state.setdefault("artifacts", {})
        artifacts.setdefault("accepted_strategies", {})["final"] = {
            "attempt_id": best_attempt_id,
            "strategy_path": best_strategy_path,
            "metrics_path": best_metrics_path,
            "score": best_score,
            "selected_at": now,
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "SignalRunManager",
                "event": "final_selection_completed" if status == "success" else "final_selection_updated",
                "summary": f"最终选择 {best_attempt_id}",
                "attempt_id": best_attempt_id,
                "score": best_score,
            }
        )
        state["updated_at"] = now
        self.save_state(run_state_path, state)
        return state

    def update_critic_review(
        self,
        run_state_path: str | Path,
        *,
        attempt_id: str,
        critic_review_path: str,
        critic_review_md_path: str,
        next_action_path: str,
        summary: str | None = None,
        status: str = "reviewed",
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        attempts = state.setdefault("attempts", [])
        attempt = next((item for item in attempts if item.get("attempt_id") == attempt_id), None)
        if attempt is None:
            raise ValueError(f"run_state.json 中不存在 attempt：{attempt_id}")
        now = self._now_iso()
        attempt.update(
            {
                "status": status,
                "critic_review_path": critic_review_path,
                "critic_review_md_path": critic_review_md_path,
                "next_action_path": next_action_path,
                "updated_at": now,
            }
        )
        if status in {"reviewed", "failed"}:
            attempt["finished_at"] = now
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "CriticAgent",
                "event": "critic_review_completed" if status == "reviewed" else "critic_review_updated",
                "summary": summary or f"{attempt_id} 已完成复盘。",
                "attempt_id": attempt_id,
                "critic_review_path": critic_review_path,
                "critic_review_md_path": critic_review_md_path,
                "next_action_path": next_action_path,
            }
        )
        state["updated_at"] = now
        self.save_state(run_state_path, state)
        return state

    def register_run_report(
        self,
        run_state_path: str | Path,
        *,
        report_key: str,
        report_path: str,
        report_type: str,
        summary: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state(run_state_path)
        now = self._now_iso()
        payload = {
            "report_type": report_type,
            "path": report_path,
            "summary": summary,
            "created_at": now,
            **(extra or {}),
        }
        state.setdefault("artifacts", {}).setdefault("run_reports", {})[report_key] = payload
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "CriticAgent",
                "event": f"{report_type}_registered",
                "summary": summary or f"登记 run 级报告 {report_key}",
                "report_key": report_key,
                "report_path": report_path,
            }
        )
        state["updated_at"] = now
        self.save_state(run_state_path, state)
        return state

    def _build_paths(self, run_id: str) -> SignalRunPaths:
        root_dir = self.config.root_dir / "artifacts" / "signal_runs" / run_id
        return SignalRunPaths(
            run_id=run_id,
            root_dir=root_dir,
            run_state_path=root_dir / "run_state.json",
            data_dir=root_dir / "data",
            market_profile_dir=root_dir / "market_profile",
            attempts_dir=root_dir / "attempts",
            strategies_dir=root_dir / "strategies",
            reports_dir=root_dir / "reports",
            logs_dir=root_dir / "logs",
        )

    def _build_attempt_paths(self, state: dict[str, Any], attempt_id: str) -> SignalAttemptPaths:
        attempts_root = self._resolve_path(state["directories"]["attempts"])
        attempt_dir = attempts_root / attempt_id
        backtests_dir = attempt_dir / "backtests"
        return SignalAttemptPaths(
            run_id=str(state["run_id"]),
            attempt_id=attempt_id,
            attempt_dir=attempt_dir,
            strategy_dir=attempt_dir / "strategy",
            optimization_dir=attempt_dir / "optimization",
            backtests_dir=backtests_dir,
            full_backtest_dir=backtests_dir / "full",
            train_backtest_dir=backtests_dir / "train",
            validation_backtest_dir=backtests_dir / "validation",
            walk_forward_dir=backtests_dir / "walk_forward",
            review_dir=attempt_dir / "review",
            logs_dir=attempt_dir / "logs",
        )

    def _upsert_attempt_record(self, state: dict[str, Any], attempt_record: dict[str, Any]) -> None:
        attempts = state.setdefault("attempts", [])
        for index, item in enumerate(attempts):
            if item.get("attempt_id") == attempt_record["attempt_id"]:
                attempts[index] = {**item, **attempt_record}
                return
        attempts.append(attempt_record)

    def _relative_directories(self, paths: SignalRunPaths) -> dict[str, str]:
        return {
            "root": self._relative(paths.root_dir),
            "data": self._relative(paths.data_dir),
            "market_profile": self._relative(paths.market_profile_dir),
            "attempts": self._relative(paths.attempts_dir),
            "strategies": self._relative(paths.strategies_dir),
            "reports": self._relative(paths.reports_dir),
            "logs": self._relative(paths.logs_dir),
        }

    def _load_backtest_config(self, backtest_overrides: dict[str, Any] | None) -> dict[str, Any]:
        base = dict(load_config_file("backtest").get("backtest", {}))
        overrides = {key: value for key, value in (backtest_overrides or {}).items() if value is not None}
        base.update(overrides)
        return base

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _state_lock(self, run_state_path: str | Path) -> threading.RLock:
        key = str(self._resolve_path(run_state_path).resolve()).lower()
        with self._locks_guard:
            lock = self._state_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._state_locks[key] = lock
            return lock

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(path)

    def _timezone(self) -> ZoneInfo:
        return ZoneInfo(self.config.project.default_timezone)

    def _now_iso(self) -> str:
        return datetime.now(self._timezone()).isoformat()

    def _safe_name(self, value: str) -> str:
        text = value.strip().replace(".", "_")
        text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text or "asset"
