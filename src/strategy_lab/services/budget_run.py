from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.config.loader import load_config_file


class BudgetRunPaths(BaseModel):
    budget_run_id: str
    root_dir: Path
    state_path: Path
    data_dir: Path
    profile_dir: Path
    policies_dir: Path
    attempts_dir: Path
    reports_dir: Path
    logs_dir: Path
    signal_artifacts_dir: Path
    asset_count: int = 0
    asset_pool_manifest_path: Path | None = None
    signal_artifacts_manifest_path: Path | None = None
    collection_status: str = "pending"
    warnings: list[str] = []


class BudgetRunManager:
    """预算层任务运行状态管理器。"""

    _locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _state_locks: ClassVar[dict[str, threading.RLock]] = {}

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()

    def create_run(
        self,
        *,
        mode: str | None = None,
        pool_name: str | None = None,
        task_description: str | None = None,
        task_name: str | None = None,
        source_paths: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "1d",
        run_id: str | None = None,
        benchmark: str | None = None,
        strategy_max_iterations: int | None = None,
        backtest_overrides: dict[str, Any] | None = None,
    ) -> BudgetRunPaths:
        budget_cfg = self._load_budget_config()
        actual_mode = mode or str(budget_cfg.get("default_mode") or "standalone")
        actual_pool_name = pool_name or str(budget_cfg.get("default_pool_name") or "custom")
        actual_run_id = run_id or self.new_run_id(pool_name=actual_pool_name)
        paths = self._build_paths(actual_run_id)

        for directory in [
            paths.root_dir,
            paths.data_dir,
            paths.profile_dir,
            paths.policies_dir,
            paths.attempts_dir,
            paths.reports_dir,
            paths.logs_dir,
            paths.signal_artifacts_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

        state = self.build_initial_state(
            paths=paths,
            mode=actual_mode,
            pool_name=actual_pool_name,
            task_description=task_description,
            task_name=task_name,
            source_paths=source_paths or [],
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            benchmark=benchmark,
            strategy_max_iterations=strategy_max_iterations,
            backtest_overrides=backtest_overrides,
        )
        self._collect_signal_runs(
            paths,
            state,
            source_paths or [],
            requested_start_date=start_date,
            requested_end_date=end_date,
            frequency=frequency,
        )
        self.save_state(paths.state_path, state)
        return paths

    def build_initial_state(
        self,
        *,
        paths: BudgetRunPaths,
        mode: str,
        pool_name: str,
        task_description: str | None,
        task_name: str | None,
        source_paths: list[str],
        start_date: str | None,
        end_date: str | None,
        frequency: str,
        benchmark: str | None,
        strategy_max_iterations: int | None,
        backtest_overrides: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        budget_cfg = self._load_budget_config()
        actual_benchmark = benchmark or str(budget_cfg.get("benchmark") or "equal_weight_rebalance")
        description = task_description or f"为 {pool_name} 训练预算层策略"
        state = {
            "schema_version": "0.1.0",
            "budget_run_id": paths.budget_run_id,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "mode": mode,
            "task": {
                "task_name": task_name or paths.budget_run_id,
                "description": description,
                "pool_name": pool_name,
                "benchmark": actual_benchmark,
                "data_range": {
                    "start": start_date,
                    "end": end_date,
                    "frequency": frequency,
                },
                "objective": {
                    "primary": "sharpe",
                    "secondary": [
                        "walk_forward_stability",
                        "max_drawdown_control",
                        "turnover_control",
                        "diversification",
                    ],
                },
            },
            "directories": self._relative_directories(paths),
            "asset_pool": {
                "status": "pending",
                "source_paths": source_paths,
                "symbols": [],
                "asset_metadata_path": None,
                "summary": None,
                "error": None,
            },
            "signal_artifacts": {
                "status": "pending",
                "source_dirs": [],
                "manifest_path": None,
                "summary": None,
                "error": None,
            },
            "data_panel": {
                "status": "pending",
                "panel_ohlcv": None,
                "returns_wide": None,
                "manifest_path": None,
                "start_date": None,
                "end_date": None,
                "symbols": [],
                "summary": None,
                "error": None,
            },
            "data_split": {
                "status": "pending",
                "split_manifest": None,
                "summary": None,
                "error": None,
            },
            "budget_profile": {
                "status": "pending",
                "profile_json": None,
                "profile_md": None,
                "charts": [],
                "summary": None,
                "error": None,
            },
            "strategy_search": {
                "status": "pending",
                "max_iterations": self._resolve_strategy_max_iterations(strategy_max_iterations),
                "current_iteration": 0,
                "attempt_ids": [],
                "best_attempt_id": None,
                "best_score": None,
            },
            "final_selection": {
                "status": "pending",
                "attempt_id": None,
                "policy_path": None,
                "reason": None,
                "selected_at": None,
            },
            "backtest_config": self._load_backtest_config(backtest_overrides=backtest_overrides),
            "attempts": [],
            "artifacts": {
                "datasets": {},
                "profile": {},
                "policies": {},
                "run_reports": {},
            },
            "events": [
                {
                    "timestamp": now,
                    "actor": "BudgetRunManager",
                    "event": "budget_run_created",
                    "summary": f"创建预算层任务 {paths.budget_run_id}",
                    "mode": mode,
                    "pool_name": pool_name,
                }
            ],
        }
        return state

    def _collect_signal_runs(
        self,
        paths: BudgetRunPaths,
        state: dict[str, Any],
        source_paths: list[str],
        *,
        requested_start_date: str | None,
        requested_end_date: str | None,
        frequency: str,
    ) -> None:
        if not source_paths:
            return
        records: dict[str, dict[str, Any]] = {}
        incomplete: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw_path in source_paths:
            path = self._resolve_path(raw_path)
            if not path.exists():
                warnings.append(f"信号层结果路径不存在：{raw_path}")
                continue
            run_state_paths = [path] if path.is_file() and path.name == "run_state.json" else list(path.rglob("run_state.json"))
            for run_state_path in run_state_paths:
                record, missing = self._read_signal_artifact_record(run_state_path)
                if record is None:
                    continue
                if missing:
                    incomplete.append(
                        {
                            "symbol": record.get("symbol"),
                            "run_id": record.get("run_id"),
                            "run_state_path": self._relative(run_state_path),
                            "missing": missing,
                        }
                    )
                    continue
                symbol = str(record["symbol"])
                existing = records.get(symbol)
                if existing is None or str(record.get("updated_at") or "") > str(existing.get("updated_at") or ""):
                    records[symbol] = record

        copied_records = []
        for symbol, record in records.items():
            target_dir = paths.signal_artifacts_dir / self._safe_name(symbol)
            target_dir.mkdir(parents=True, exist_ok=True)
            record["copied_files"] = self._copy_signal_small_files(record, target_dir, warnings)
            copied_records.append(record)

        symbols = sorted(records)
        resolved_range = self._resolve_budget_data_range(
            records=records,
            requested_start_date=requested_start_date,
            requested_end_date=requested_end_date,
            frequency=frequency,
            supplemental_data_dir=paths.data_dir / "supplemental",
        )
        asset_manifest_path = paths.data_dir / "asset_pool_manifest.json"
        signal_manifest_path = paths.signal_artifacts_dir / "signal_artifacts_manifest.json"
        asset_manifest = {
            "budget_run_id": paths.budget_run_id,
            "status": "success" if records and not incomplete and not warnings else ("partial" if records else "failed"),
            "asset_count": len(symbols),
            "symbols": symbols,
            "data_range": resolved_range,
            "data_coverage": resolved_range.get("coverage_by_symbol", []),
            "assets": [
                {
                    "symbol": item["symbol"],
                    "asset_type": item.get("asset_type"),
                    "signal_run_id": item.get("run_id"),
                    "signal_run_state_path": item.get("run_state_path"),
                    "data_range": item.get("data_range"),
                    "primary_dataset": item.get("primary_dataset"),
                    "selected_attempt_id": item.get("selected_attempt_id"),
                    "selected_strategy_path": item.get("selected_strategy_path"),
                    "selected_metrics_path": item.get("selected_metrics_path"),
                    "copied_dir": self._relative(paths.signal_artifacts_dir / self._safe_name(str(item["symbol"]))),
                    "required_data_output_dir": self._relative(paths.data_dir / "supplemental" / self._safe_name(str(item["symbol"]))),
                    "data_coverage": resolved_range.get("coverage_map", {}).get(str(item["symbol"])),
                }
                for item in copied_records
            ],
            "source_paths": source_paths,
            "signal_manifest_path": self._relative(signal_manifest_path),
            "incomplete_signal_runs": incomplete,
            "warnings": warnings,
        }
        signal_manifest = {
            "budget_run_id": paths.budget_run_id,
            "status": asset_manifest["status"],
            "count": len(copied_records),
            "data_range": resolved_range,
            "records": copied_records,
            "incomplete_signal_runs": incomplete,
            "warnings": warnings,
        }
        asset_manifest_path.write_text(json.dumps(asset_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        signal_manifest_path.write_text(json.dumps(signal_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        status = asset_manifest["status"]
        artifact_status = status
        if status == "success" and resolved_range.get("missing_data"):
            status = "partial"
            asset_manifest["status"] = status
            signal_manifest["status"] = status
            asset_manifest_path.write_text(json.dumps(asset_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            signal_manifest_path.write_text(json.dumps(signal_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        state["status"] = "created" if status in {"success", "partial"} else "failed"
        state.setdefault("task", {})["data_range"] = {
            "start": resolved_range.get("start"),
            "end": resolved_range.get("end"),
            "frequency": frequency,
            "source": resolved_range.get("source"),
            "policy": resolved_range.get("policy"),
        }
        state.setdefault("asset_pool", {}).update(
            {
                "status": status,
                "source_paths": source_paths,
                "symbols": symbols,
                "asset_metadata_path": self._relative(asset_manifest_path),
                "summary": f"从信号层结果目录整理出 {len(symbols)} 个完整资产。",
                "error": self._format_collection_errors(
                    incomplete,
                    warnings,
                    resolved_range.get("missing_data", []),
                ) if status in {"partial", "failed"} else None,
            }
        )
        state.setdefault("signal_artifacts", {}).update(
            {
                "status": artifact_status,
                "source_dirs": source_paths,
                "manifest_path": self._relative(signal_manifest_path),
                "summary": f"复制并登记 {len(copied_records)} 个资产的信号层小文件。",
                "error": self._format_collection_errors(incomplete, warnings) if artifact_status in {"partial", "failed"} else None,
            }
        )
        state.setdefault("data_panel", {}).update(
            {
                "status": "pending" if not resolved_range.get("missing_data") else "needs_data",
                "start_date": resolved_range.get("start"),
                "end_date": resolved_range.get("end"),
                "symbols": symbols,
                "missing_data": resolved_range.get("missing_data", []),
                "coverage": resolved_range.get("coverage_by_symbol", []),
                "summary": (
                    "信号层数据覆盖预算层数据范围。"
                    if not resolved_range.get("missing_data")
                    else f"有 {len(resolved_range.get('missing_data', []))} 个资产存在数据范围缺口，需要补齐后再汇总 panel。"
                ),
            }
        )
        state.setdefault("artifacts", {}).setdefault("datasets", {})["asset_pool"] = {
            "manifest": self._relative(asset_manifest_path),
            "asset_count": len(symbols),
            "symbols": symbols,
        }
        state.setdefault("artifacts", {}).setdefault("run_reports", {})["signal_artifacts_manifest"] = {
            "report_type": "signal_artifacts_manifest",
            "path": self._relative(signal_manifest_path),
            "summary": f"登记 {len(copied_records)} 个资产的信号层小文件。",
            "created_at": self._now_iso(),
        }
        state.setdefault("events", []).append(
            {
                "timestamp": self._now_iso(),
                "actor": "BudgetRunManager",
                "event": "signal_artifacts_collected" if status != "failed" else "signal_artifacts_collection_failed",
                "summary": f"信号层结果收集状态：{status}，完整资产数：{len(symbols)}。",
                "asset_count": len(symbols),
                "incomplete_count": len(incomplete),
                "missing_data_count": len(resolved_range.get("missing_data", [])),
            }
        )
        state["updated_at"] = self._now_iso()
        paths.asset_count = len(symbols)
        paths.asset_pool_manifest_path = asset_manifest_path
        paths.signal_artifacts_manifest_path = signal_manifest_path
        paths.collection_status = str(status)
        paths.warnings = warnings + [
            f"{item.get('symbol') or item.get('run_id')} 缺少：{', '.join(item.get('missing', []))}"
            for item in incomplete
        ] + [
            f"{item.get('symbol')} 缺数据：{item.get('missing_start')} 至 {item.get('missing_end')}，建议输出到 {item.get('suggested_output_dir')}"
            for item in resolved_range.get("missing_data", [])
        ]

    def _resolve_budget_data_range(
        self,
        *,
        records: dict[str, dict[str, Any]],
        requested_start_date: str | None,
        requested_end_date: str | None,
        frequency: str,
        supplemental_data_dir: Path,
    ) -> dict[str, Any]:
        ranges: list[dict[str, Any]] = []
        for symbol, record in records.items():
            data_range = record.get("data_range") or {}
            start = data_range.get("start")
            end = data_range.get("end")
            if start and end:
                ranges.append({"symbol": symbol, "start": str(start), "end": str(end)})
        if requested_start_date or requested_end_date:
            start = requested_start_date or max((item["start"] for item in ranges), default=None)
            end = requested_end_date or min((item["end"] for item in ranges), default=None)
            source = "user_requested"
            policy = "user_range_with_gap_check"
        else:
            start = max((item["start"] for item in ranges), default=None)
            end = min((item["end"] for item in ranges), default=None)
            source = "signal_runs_intersection"
            policy = "intersection"

        missing_data: list[dict[str, Any]] = []
        coverage_by_symbol: list[dict[str, Any]] = []
        coverage_map: dict[str, dict[str, Any]] = {}
        if start and end:
            for symbol, record in records.items():
                data_range = record.get("data_range") or {}
                asset_start = data_range.get("start")
                asset_end = data_range.get("end")
                supplemental_dir = supplemental_data_dir / self._safe_name(symbol)
                missing_segments: list[dict[str, Any]] = []
                if not asset_start or not asset_end:
                    payload = self._missing_data_payload(
                            symbol=symbol,
                            missing_start=start,
                            missing_end=end,
                            reason="signal run 缺少 data_range",
                            supplemental_dir=supplemental_dir,
                            primary_dataset=record.get("primary_dataset"),
                    )
                    missing_data.append(payload)
                    missing_segments.append(payload)
                    coverage_status = "unknown"
                    coverage_reason = "signal run 缺少 data_range，无法判断原始数据覆盖范围。"
                    coverage = self._coverage_payload(
                        symbol=symbol,
                        requested_start=start,
                        requested_end=end,
                        asset_start=asset_start,
                        asset_end=asset_end,
                        primary_dataset=record.get("primary_dataset"),
                        supplemental_dir=supplemental_dir,
                        status=coverage_status,
                        reason=coverage_reason,
                        missing_segments=missing_segments,
                    )
                    coverage_by_symbol.append(coverage)
                    coverage_map[symbol] = coverage
                    continue
                if str(asset_start) > str(start):
                    payload = self._missing_data_payload(
                            symbol=symbol,
                            missing_start=start,
                            missing_end=str(asset_start),
                            reason="用户要求的开始日期早于信号层数据开始日期",
                            supplemental_dir=supplemental_dir,
                            primary_dataset=record.get("primary_dataset"),
                    )
                    missing_data.append(payload)
                    missing_segments.append(payload)
                if str(asset_end) < str(end):
                    payload = self._missing_data_payload(
                            symbol=symbol,
                            missing_start=str(asset_end),
                            missing_end=end,
                            reason="用户要求的结束日期晚于信号层数据结束日期",
                            supplemental_dir=supplemental_dir,
                            primary_dataset=record.get("primary_dataset"),
                    )
                    missing_data.append(payload)
                    missing_segments.append(payload)
                if missing_segments:
                    coverage_status = "missing"
                    coverage_reason = "信号层 primary_dataset 未完整覆盖预算层要求范围。"
                else:
                    coverage_status = "covered"
                    coverage_reason = "信号层 primary_dataset 已覆盖预算层要求范围。"
                coverage = self._coverage_payload(
                    symbol=symbol,
                    requested_start=start,
                    requested_end=end,
                    asset_start=asset_start,
                    asset_end=asset_end,
                    primary_dataset=record.get("primary_dataset"),
                    supplemental_dir=supplemental_dir,
                    status=coverage_status,
                    reason=coverage_reason,
                    missing_segments=missing_segments,
                )
                coverage_by_symbol.append(coverage)
                coverage_map[symbol] = coverage
        return {
            "start": start,
            "end": end,
            "frequency": frequency,
            "source": source,
            "policy": policy,
            "asset_ranges": ranges,
            "missing_data": missing_data,
            "coverage_by_symbol": coverage_by_symbol,
            "coverage_map": coverage_map,
        }

    def _missing_data_payload(
        self,
        *,
        symbol: str,
        missing_start: str,
        missing_end: str,
        reason: str,
        supplemental_dir: Path,
        primary_dataset: str | None = None,
    ) -> dict[str, Any]:
        symbol_safe = self._safe_name(symbol)
        file_start = str(missing_start).replace("-", "")
        file_end = str(missing_end).replace("-", "")
        return {
            "symbol": symbol,
            "missing_start": missing_start,
            "missing_end": missing_end,
            "reason": reason,
            "primary_dataset": primary_dataset,
            "suggested_output_dir": self._relative(supplemental_dir),
            "suggested_file_name": f"{symbol_safe}_{file_start}_{file_end}_supplement.parquet",
            "expected_columns": ["symbol", "datetime", "open", "high", "low", "close", "volume", "pctchange"],
            "expected_frequency": "1d",
        }

    def _coverage_payload(
        self,
        *,
        symbol: str,
        requested_start: str,
        requested_end: str,
        asset_start: str | None,
        asset_end: str | None,
        primary_dataset: str | None,
        supplemental_dir: Path,
        status: str,
        reason: str,
        missing_segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "requested_start": requested_start,
            "requested_end": requested_end,
            "available_start": asset_start,
            "available_end": asset_end,
            "primary_dataset": primary_dataset,
            "supplemental_dir": self._relative(supplemental_dir),
            "missing_segments": missing_segments,
        }

    def _read_signal_artifact_record(self, run_state_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            signal_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None, ["run_state.json 无法读取"]

        symbol = signal_state.get("task", {}).get("asset", {}).get("symbol")
        if not symbol:
            return None, ["task.asset.symbol"]
        final = signal_state.get("steps", {}).get("final_selection", {}) or {}
        accepted = signal_state.get("artifacts", {}).get("accepted_strategies", {}).get("final", {}) or {}
        strategy_path = final.get("selected_strategy_path") or accepted.get("strategy_path")
        metrics_path = final.get("selected_metrics_path") or accepted.get("metrics_path")
        attempt_id = final.get("selected_attempt_id") or accepted.get("attempt_id")
        primary = signal_state.get("artifacts", {}).get("datasets", {}).get("primary", {}) or {}
        primary_dataset = signal_state.get("steps", {}).get("data_acquisition", {}).get("primary_dataset") or primary.get("file")

        missing: list[str] = []
        if not primary_dataset:
            missing.append("primary_dataset")
        elif not self._resolve_path(primary_dataset).exists():
            missing.append(f"primary_dataset 文件不存在：{primary_dataset}")
        if not strategy_path:
            missing.append("selected_strategy_path")
            strategy_dir = None
        else:
            strategy_file = self._resolve_path(strategy_path)
            if not strategy_file.exists():
                missing.append(f"strategy.py 文件不存在：{strategy_path}")
            strategy_dir = strategy_file.parent

        required_strategy_files: dict[str, Path] = {}
        if strategy_dir is not None:
            required_strategy_files = {
                "strategy_spec_path": strategy_dir / "strategy_spec.md",
                "param_space_path": strategy_dir / "param_space.json",
                "strategy_meta_path": strategy_dir / "strategy_meta.json",
            }
            for label, path in required_strategy_files.items():
                if not path.exists():
                    missing.append(f"{label} 文件不存在：{self._relative(path)}")

        memory_path = run_state_path.parent / "reports" / "signal_agent_memory.md"
        if not memory_path.exists():
            missing.append(f"signal_agent_memory.md 文件不存在：{self._relative(memory_path)}")

        record = {
            "symbol": str(symbol).strip().upper(),
            "run_id": signal_state.get("run_id") or run_state_path.parent.name,
            "run_state_path": self._relative(run_state_path),
            "updated_at": signal_state.get("updated_at"),
            "asset_type": signal_state.get("task", {}).get("asset", {}).get("asset_type"),
            "data_range": signal_state.get("task", {}).get("data_range", {}),
            "primary_dataset": primary_dataset,
            "selected_attempt_id": attempt_id,
            "selected_strategy_path": strategy_path,
            "selected_metrics_path": metrics_path,
            "strategy_spec_path": self._relative(required_strategy_files.get("strategy_spec_path")) if required_strategy_files else None,
            "param_space_path": self._relative(required_strategy_files.get("param_space_path")) if required_strategy_files else None,
            "strategy_meta_path": self._relative(required_strategy_files.get("strategy_meta_path")) if required_strategy_files else None,
            "signal_agent_memory_path": self._relative(memory_path) if memory_path.exists() else None,
            "score": accepted.get("score"),
        }
        if metrics_path and not self._resolve_path(metrics_path).exists():
            record["selected_metrics_path"] = None
        return record, missing

    def _copy_signal_small_files(self, record: dict[str, Any], target_dir: Path, warnings: list[str]) -> dict[str, str]:
        copied: dict[str, str] = {}
        for key, filename in {
            "run_state_path": "run_state.json",
            "selected_strategy_path": "strategy.py",
            "strategy_spec_path": "strategy_spec.md",
            "param_space_path": "param_space.json",
            "strategy_meta_path": "strategy_meta.json",
            "signal_agent_memory_path": "signal_agent_memory.md",
            "selected_metrics_path": "metrics.json",
        }.items():
            raw_path = record.get(key)
            if not raw_path:
                continue
            source = self._resolve_path(raw_path)
            if not source.exists() or not source.is_file():
                warnings.append(f"信号层小文件不存在，无法复制：{raw_path}")
                continue
            dest = target_dir / filename
            shutil.copy2(source, dest)
            copied[key] = self._relative(dest)
        return copied

    @staticmethod
    def _format_collection_errors(
        incomplete: list[dict[str, Any]],
        warnings: list[str],
        missing_data: list[dict[str, Any]] | None = None,
    ) -> str | None:
        messages = list(warnings)
        for item in incomplete:
            missing = ", ".join(item.get("missing", []))
            messages.append(f"{item.get('symbol') or item.get('run_id')} 缺少：{missing}")
        for item in missing_data or []:
            messages.append(
                f"{item.get('symbol')} 缺数据：{item.get('missing_start')} 至 {item.get('missing_end')}；"
                f"原因：{item.get('reason')}；建议输出到：{item.get('suggested_output_dir')}"
            )
        return "; ".join(messages) if messages else None

    def new_run_id(self, pool_name: str) -> str:
        timestamp = datetime.now(self._timezone()).strftime("%Y%m%d_%H%M%S")
        return f"budget_{self._safe_name(pool_name)}_{timestamp}"

    def load_state(self, state_path: str | Path) -> dict[str, Any]:
        path = self._resolve_path(state_path)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state(self, state_path: str | Path, state: dict[str, Any]) -> None:
        path = self._resolve_path(state_path)
        with self._state_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def append_event(
        self,
        state_path: str | Path,
        *,
        actor: str,
        event: str,
        summary: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state(state_path)
        payload = {
            "timestamp": self._now_iso(),
            "actor": actor,
            "event": event,
            "summary": summary,
            **(extra or {}),
        }
        state.setdefault("events", []).append(payload)
        state["updated_at"] = payload["timestamp"]
        self.save_state(state_path, state)
        return state

    def update_policy_review(
        self,
        state_path: str | Path,
        *,
        search_id: str,
        critic_review_path: str,
        critic_review_md_path: str,
        next_action_path: str | None = None,
        summary: str | None = None,
        status: str = "reviewed",
    ) -> dict[str, Any]:
        state = self.load_state(state_path)
        searches = state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("searches", {})
        search = searches.setdefault(search_id, {})
        now = self._now_iso()
        search.update(
            {
                "status": status,
                "critic_review_path": critic_review_path,
                "critic_review_md_path": critic_review_md_path,
                "next_action_path": next_action_path,
                "reviewed_at": now,
            }
        )
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetCriticAgent",
                "event": "budget_policy_review_completed" if status == "reviewed" else "budget_policy_review_updated",
                "summary": summary or f"预算策略 {search_id} 已完成复盘。",
                "search_id": search_id,
                "critic_review_path": critic_review_path,
                "critic_review_md_path": critic_review_md_path,
                "next_action_path": next_action_path,
            }
        )
        state["updated_at"] = now
        self.save_state(state_path, state)
        return state

    def register_run_report(
        self,
        state_path: str | Path,
        *,
        report_key: str,
        report_path: str,
        report_type: str,
        summary: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state(state_path)
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
                "actor": "BudgetCriticAgent",
                "event": f"{report_type}_registered",
                "summary": summary or f"登记预算层 run 级报告 {report_key}",
                "report_key": report_key,
                "report_path": report_path,
            }
        )
        state["updated_at"] = now
        self.save_state(state_path, state)
        return state

    def update_final_selection(
        self,
        state_path: str | Path,
        *,
        search_id: str,
        reason: str,
        selection_report_path: str | Path | None = None,
        report_key: str | None = None,
        status: str = "success",
        best_score: float | None = None,
    ) -> dict[str, Any]:
        state_path = self._resolve_path(state_path)
        state = self.load_state(state_path)
        now = self._now_iso()
        searches = state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("searches", {})
        search = searches.get(search_id)
        if not search:
            available = ", ".join(sorted(searches.keys())[:20])
            suffix = f"可用 search_id: {available}" if available else "当前没有已登记的预算策略搜索结果。"
            raise ValueError(f"budget_run_state.json 中不存在 search_id: {search_id}。{suffix}")

        if best_score is None:
            best_score = self._optional_float(search.get("best_score"))

        best_policy_config_path = search.get("best_policy_config_path")
        search_result_path = search.get("search_result_path")
        attempt_summary_path = search.get("attempt_summary_path")
        attempt_summary_md_path = search.get("attempt_summary_md_path")
        stage_attribution_path = search.get("stage_attribution_path")
        stage_attribution_md_path = search.get("stage_attribution_md_path")
        stage_attribution_chart_path = search.get("stage_attribution_chart_path")
        summary = search.get("summary") if isinstance(search.get("summary"), dict) else {}
        policy_name = self._policy_name_from_selection(best_policy_config_path, attempt_summary_path, search_id)

        actual_report_path = self._resolve_final_report_path(
            state=state,
            search_id=search_id,
            selection_report_path=selection_report_path,
        )
        actual_report_path.parent.mkdir(parents=True, exist_ok=True)
        actual_report_path.write_text(
            self._format_final_selection_report(
                state=state,
                search_id=search_id,
                policy_name=policy_name,
                best_score=best_score,
                reason=reason,
                search=search,
                report_path=actual_report_path,
                selected_at=now,
            ),
            encoding="utf-8",
        )
        relative_report_path = self._relative(actual_report_path)
        actual_report_key = report_key or actual_report_path.stem

        final_selection = {
            "status": status,
            "search_id": search_id,
            "attempt_id": search_id,
            "policy_name": policy_name,
            "policy_path": best_policy_config_path,
            "policy_config_path": best_policy_config_path,
            "search_result_path": search_result_path,
            "attempt_summary_path": attempt_summary_path,
            "attempt_summary_md_path": attempt_summary_md_path,
            "stage_attribution_path": stage_attribution_path,
            "stage_attribution_md_path": stage_attribution_md_path,
            "stage_attribution_chart_path": stage_attribution_chart_path,
            "selection_report_path": relative_report_path,
            "reason": reason,
            "best_score": best_score,
            "summary": summary,
            "selected_at": now,
        }
        state["final_selection"] = final_selection

        strategy_search = state.setdefault("strategy_search", {})
        if status == "success":
            strategy_search["status"] = "completed"
            state["status"] = "completed"
        strategy_search["best_attempt_id"] = search_id
        strategy_search["best_search_id"] = search_id
        if best_score is not None:
            strategy_search["best_score"] = best_score

        state.setdefault("artifacts", {}).setdefault("policies", {})["final"] = final_selection
        state.setdefault("artifacts", {}).setdefault("run_reports", {})[actual_report_key] = {
            "report_type": "budget_final_selection",
            "path": relative_report_path,
            "summary": f"最终选择预算策略 {policy_name or search_id}。",
            "search_id": search_id,
            "policy_name": policy_name,
            "best_score": best_score,
            "created_at": now,
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetRunManager",
                "event": "budget_final_selection_completed" if status == "success" else "budget_final_selection_updated",
                "summary": f"最终选择预算策略 {policy_name or search_id}。",
                "search_id": search_id,
                "best_score": best_score,
                "selection_report_path": relative_report_path,
            }
        )
        state["updated_at"] = now
        self.save_state(state_path, state)
        return state

    def _build_paths(self, budget_run_id: str) -> BudgetRunPaths:
        root_dir = self.config.root_dir / "artifacts" / "budget_runs" / budget_run_id
        return BudgetRunPaths(
            budget_run_id=budget_run_id,
            root_dir=root_dir,
            state_path=root_dir / "budget_run_state.json",
            data_dir=root_dir / "data",
            profile_dir=root_dir / "profile",
            policies_dir=root_dir / "policies",
            attempts_dir=root_dir / "attempts",
            reports_dir=root_dir / "reports",
            logs_dir=root_dir / "logs",
            signal_artifacts_dir=root_dir / "signal_artifacts",
        )

    def _relative_directories(self, paths: BudgetRunPaths) -> dict[str, str]:
        return {
            "root": self._relative(paths.root_dir),
            "data": self._relative(paths.data_dir),
            "profile": self._relative(paths.profile_dir),
            "policies": self._relative(paths.policies_dir),
            "attempts": self._relative(paths.attempts_dir),
            "reports": self._relative(paths.reports_dir),
            "logs": self._relative(paths.logs_dir),
            "signal_artifacts": self._relative(paths.signal_artifacts_dir),
        }

    def _load_budget_config(self) -> dict[str, Any]:
        return dict(load_config_file("budget").get("budget", {}))

    def _load_backtest_config(self, backtest_overrides: dict[str, Any] | None) -> dict[str, Any]:
        budget_cfg = self._load_budget_config()
        base = dict(budget_cfg.get("backtest", {}))
        overrides = {key: value for key, value in (backtest_overrides or {}).items() if value is not None}
        base.update(overrides)
        return base

    def _resolve_strategy_max_iterations(self, strategy_max_iterations: int | None) -> int:
        if strategy_max_iterations is not None:
            return int(strategy_max_iterations)
        budget_cfg = self._load_budget_config()
        return int(budget_cfg.get("max_strategy_iterations") or 20)

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _state_lock(self, state_path: str | Path) -> threading.RLock:
        key = str(self._resolve_path(state_path).resolve()).lower()
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
        return text or "pool"

    def _optional_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _read_json_if_exists(self, path: str | Path | None) -> dict[str, Any]:
        if not path:
            return {}
        actual_path = self._resolve_path(path)
        if not actual_path.exists():
            return {}
        try:
            return json.loads(actual_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _policy_name_from_selection(
        self,
        best_policy_config_path: str | Path | None,
        attempt_summary_path: str | Path | None,
        fallback: str,
    ) -> str:
        policy_config = self._read_json_if_exists(best_policy_config_path)
        if policy_config.get("policy_name"):
            return str(policy_config["policy_name"])
        if policy_config.get("policy_id"):
            return str(policy_config["policy_id"])
        attempt_summary = self._read_json_if_exists(attempt_summary_path)
        policy = attempt_summary.get("policy", {}) if isinstance(attempt_summary.get("policy"), dict) else {}
        for key in ["policy_name", "policy_id"]:
            if policy.get(key):
                return str(policy[key])
        return fallback

    def _resolve_final_report_path(
        self,
        *,
        state: dict[str, Any],
        search_id: str,
        selection_report_path: str | Path | None,
    ) -> Path:
        if selection_report_path:
            return self._resolve_path(selection_report_path)
        reports_dir = state.get("directories", {}).get("reports")
        base_dir = self._resolve_path(reports_dir) if reports_dir else self.config.root_dir / "artifacts" / "budget_runs" / str(state.get("budget_run_id")) / "reports"
        safe_search_id = self._safe_name(search_id)
        timestamp = datetime.now(self._timezone()).strftime("%Y%m%d_%H%M%S")
        return base_dir / f"budget_final_selection_{safe_search_id}_{timestamp}.md"

    def _format_final_selection_report(
        self,
        *,
        state: dict[str, Any],
        search_id: str,
        policy_name: str,
        best_score: float | None,
        reason: str,
        search: dict[str, Any],
        report_path: Path,
        selected_at: str,
    ) -> str:
        summary = search.get("summary") if isinstance(search.get("summary"), dict) else {}
        lines = [
            "# 预算层最终选择",
            "",
            f"- budget_run_id: {state.get('budget_run_id')}",
            f"- selected_at: {selected_at}",
            f"- search_id: {search_id}",
            f"- policy_name: {policy_name}",
            f"- best_score: {best_score}",
            f"- report_path: {self._relative(report_path)}",
            "",
            "## 选择理由",
            "",
            reason.strip() or "未填写选择理由。",
            "",
            "## 关键指标",
            "",
        ]
        metric_keys = [
            "full_total_return",
            "full_sharpe",
            "validation_total_return",
            "validation_sharpe",
            "walk_forward_mean_score",
        ]
        for key in metric_keys:
            lines.append(f"- {key}: {summary.get(key)}")
        lines.extend(
            [
                "",
                "## 关键文件",
                "",
                f"- best_policy_config_path: {search.get('best_policy_config_path')}",
                f"- search_result_path: {search.get('search_result_path')}",
                f"- attempt_summary_path: {search.get('attempt_summary_path')}",
                f"- attempt_summary_md_path: {search.get('attempt_summary_md_path')}",
                f"- stage_attribution_path: {search.get('stage_attribution_path')}",
                f"- stage_attribution_md_path: {search.get('stage_attribution_md_path')}",
                f"- stage_attribution_chart_path: {search.get('stage_attribution_chart_path')}",
                "",
                "## 检查结论",
                "",
                "- 已确认 selected search_id 存在于 budget_run_state.json 的 artifacts.policies.searches。",
                "- 已登记 final_selection、artifacts.policies.final、artifacts.run_reports 和 events。",
            ]
        )
        return "\n".join(lines) + "\n"
