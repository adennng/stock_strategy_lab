from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config


class PortfolioRunPaths(BaseModel):
    portfolio_run_id: str
    root_dir: Path
    state_path: Path
    source_artifacts_dir: Path
    budget_source_dir: Path
    signal_source_dir: Path
    data_dir: Path
    versions_dir: Path
    reports_dir: Path
    logs_dir: Path
    copied_signal_count: int = 0
    symbols: list[str] = Field(default_factory=list)


class PortfolioRunManager:
    """组合层任务运行状态管理器。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()

    def create_run(
        self,
        *,
        source_budget_run_path: str | Path,
        portfolio_run_id: str | None = None,
        task_description: str | None = None,
    ) -> PortfolioRunPaths:
        budget_state_path = self._resolve_budget_state_path(source_budget_run_path)
        budget_state = self._read_json(budget_state_path)
        actual_run_id = portfolio_run_id or self.new_run_id(budget_state)
        paths = self._build_paths(actual_run_id)
        if paths.root_dir.exists():
            raise FileExistsError(f"portfolio run 目录已存在：{paths.root_dir}")

        for directory in [
            paths.root_dir,
            paths.source_artifacts_dir,
            paths.budget_source_dir,
            paths.signal_source_dir,
            paths.data_dir,
            paths.versions_dir,
            paths.reports_dir,
            paths.logs_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

        selected_budget = self._resolve_selected_budget_policy(
            budget_state=budget_state,
        )
        budget_artifacts = self._copy_budget_artifacts(
            budget_state_path=budget_state_path,
            selected_budget=selected_budget,
            paths=paths,
        )
        signal_artifacts = self._copy_signal_artifacts(
            budget_state=budget_state,
            budget_state_path=budget_state_path,
            paths=paths,
        )
        state = self._build_initial_state(
            paths=paths,
            budget_state=budget_state,
            budget_state_path=budget_state_path,
            budget_artifacts=budget_artifacts,
            signal_artifacts=signal_artifacts,
            selected_budget=selected_budget,
            task_description=task_description,
        )
        self.save_state(paths.state_path, state)
        paths.copied_signal_count = len(signal_artifacts["items"])
        paths.symbols = [item["symbol"] for item in signal_artifacts["items"]]
        return paths

    def load_state(self, state_path: str | Path) -> dict[str, Any]:
        return self._read_json(self._resolve_path(state_path))

    def save_state(self, state_path: str | Path, state: dict[str, Any]) -> None:
        path = self._resolve_path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

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
                "actor": "PortfolioRunManager",
                "event": f"{report_type}_registered",
                "summary": summary or f"登记组合层 run 级报告 {report_key}",
                "report_key": report_key,
                "report_path": report_path,
            }
        )
        state["updated_at"] = now
        self.save_state(state_path, state)
        return state

    def init_fusion_version(
        self,
        state_path: str | Path,
        *,
        version_id: str,
        summary: str | None = None,
        version_role: str = "candidate",
        policy_name: str | None = None,
    ) -> dict[str, Any]:
        """Initialize a portfolio fusion version from source budget/signal snapshots."""
        state_path_actual = self._resolve_path(state_path)
        state = self.load_state(state_path_actual)
        if any(item.get("version_id") == version_id for item in state.get("versions", [])):
            raise FileExistsError(f"组合层版本已存在：{version_id}")

        versions_dir = self._resolve_path(state.get("directories", {}).get("versions") or state_path_actual.parent / "versions")
        version_dir = versions_dir / self._safe_name(version_id)
        if version_dir.exists():
            raise FileExistsError(f"目标版本目录已存在：{version_dir}")

        budget_dir = version_dir / "budget_policy"
        signals_dir = version_dir / "signal_strategies"
        evaluation_dir = version_dir / "evaluation"
        for path in [budget_dir, signals_dir, evaluation_dir]:
            path.mkdir(parents=True, exist_ok=True)

        copied_budget = self._init_version_budget_snapshot(state=state, budget_dir=budget_dir)
        copied_signals = self._init_version_signal_snapshots(state=state, signals_dir=signals_dir)
        now = self._now_iso()
        actual_policy_name = policy_name or self._safe_name(version_id)

        fusion_policy_path = version_dir / "fusion_policy.py"
        param_space_path = version_dir / "param_space.json"
        fusion_policy_spec_path = version_dir / "fusion_policy_spec.md"
        fusion_policy_meta_path = version_dir / "fusion_policy_meta.json"
        fusion_manifest_path = version_dir / "fusion_manifest.json"

        fusion_policy = self._default_fusion_policy_py(policy_name=actual_policy_name)
        param_space = self._default_fusion_param_space()
        fusion_meta = {
            "policy_name": actual_policy_name,
            "policy_version": version_id,
            "created_by": "PortfolioAgent",
            "policy_mode": "python_script",
            "fusion_type": "python_policy",
            "source_profile_path": state.get("profile", {}).get("portfolio_profile_path"),
            "primary_objective": state.get("task", {}).get("objective", {}).get("primary") or "sharpe",
            "risk_level": "balanced",
            "complexity_level": "p0",
            "created_at": now,
            "notes": summary or "系统初始化的组合层融合策略版本。PortfolioAgent 应基于画像修改 fusion_policy.py。",
        }
        fusion_spec = self._default_fusion_spec(
            state=state,
            version_id=version_id,
            policy_name=actual_policy_name,
            summary=summary,
        )
        fusion_manifest = {
            "schema_version": "0.1.0",
            "version_id": version_id,
            "created_at": now,
            "version_role": version_role,
            "source_version_id": None,
            "change_summary": summary,
            "fusion_policy_path": self._relative(fusion_policy_path),
            "fusion_policy_spec_path": self._relative(fusion_policy_spec_path),
            "param_space_path": self._relative(param_space_path),
            "fusion_policy_meta_path": self._relative(fusion_policy_meta_path),
            "budget": {
                "budget_policy_dir": self._relative(budget_dir),
                "budget_policy_config_path": self._relative(budget_dir / "budget_policy_config.json"),
            },
            "signals": copied_signals,
            "evaluation_dir": self._relative(evaluation_dir),
            "notes": summary or "组合层版本由 init-fusion-version 初始化；预算层和信号层文件是冻结快照，默认不修改。",
        }

        fusion_policy_path.write_text(fusion_policy, encoding="utf-8")
        param_space_path.write_text(json.dumps(param_space, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        fusion_policy_spec_path.write_text(fusion_spec, encoding="utf-8")
        fusion_policy_meta_path.write_text(json.dumps(fusion_meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        fusion_manifest_path.write_text(json.dumps(fusion_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        version_payload = {
            "version_id": version_id,
            "status": "initialized",
            "version_role": version_role,
            "source_version_id": None,
            "version_dir": self._relative(version_dir),
            "fusion_manifest_path": self._relative(fusion_manifest_path),
            "fusion_policy_path": self._relative(fusion_policy_path),
            "param_space_path": self._relative(param_space_path),
            "fusion_policy_spec_path": self._relative(fusion_policy_spec_path),
            "fusion_policy_meta_path": self._relative(fusion_policy_meta_path),
            "created_at": now,
            "summary": summary or "初始化组合层融合策略版本。",
        }
        state.setdefault("versions", []).append(version_payload)
        state["current_version"] = version_id
        state.setdefault("artifacts", {}).setdefault("versions", {})[version_id] = version_payload
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "PortfolioRunManager",
                "event": "portfolio_fusion_version_initialized",
                "summary": version_payload["summary"],
                "version_id": version_id,
                "fusion_manifest_path": version_payload["fusion_manifest_path"],
                "signal_count": len(copied_signals),
            }
        )
        state["updated_at"] = now
        self.save_state(state_path_actual, state)
        return {
            "status": "success",
            "portfolio_run_state_path": self._relative(state_path_actual),
            "version": version_payload,
            "version_dir": self._relative(version_dir),
            "fusion_manifest_path": self._relative(fusion_manifest_path),
            "fusion_policy_path": self._relative(fusion_policy_path),
            "param_space_path": self._relative(param_space_path),
            "fusion_policy_spec_path": self._relative(fusion_policy_spec_path),
            "fusion_policy_meta_path": self._relative(fusion_policy_meta_path),
            "budget_policy_config_path": copied_budget["budget_policy_config_path"],
            "signal_count": len(copied_signals),
            "signals": copied_signals,
            "next_step": "请基于 portfolio-profile 和 fusion_policy_library.md 修改组合层五件套，然后调用 portfolio evaluate。",
        }

    def update_final_selection(
        self,
        state_path: str | Path,
        *,
        version_id: str,
        reason: str,
        selection_report_path: str | Path | None = None,
    ) -> dict[str, Any]:
        state_path_actual = self._resolve_path(state_path)
        state = self.load_state(state_path_actual)
        version = self._find_version(state, version_id)
        version_dir = self._resolve_path(version.get("version_dir") or "")
        if not version_dir.exists():
            raise FileNotFoundError(f"组合层版本目录不存在：{version_dir}")

        now = self._now_iso()
        self._validate_final_selection_candidate(version=version, version_dir=version_dir, version_id=version_id)

        portfolio_run_id = str(state.get("portfolio_run_id") or state_path_actual.parent.name)
        reports_dir = self._resolve_path(state.get("directories", {}).get("reports") or state_path_actual.parent / "reports")
        report_path = self._resolve_path(selection_report_path) if selection_report_path else self._resolve_final_selection_report_path(
            state=state,
            version_id=version_id,
        )

        final_dir = state_path_actual.parent / "final"
        history_dir = state_path_actual.parent / "final_history"
        tmp_dir = state_path_actual.parent / f"final_tmp_{self._safe_name(version_id)}_{datetime.now(self._timezone()).strftime('%Y%m%d_%H%M%S')}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        self._copy_final_snapshot(version_dir=version_dir, final_dir=tmp_dir)
        final_manifest_path = tmp_dir / "final_manifest.json"
        selection_report_target = tmp_dir / "portfolio_final_selection.md"

        metrics_path = self._optional_relative(tmp_dir / "evaluation" / "backtest" / "metrics.json")
        fusion_manifest_path = self._optional_relative(tmp_dir / "fusion_manifest.json")
        fusion_policy_path = self._optional_relative(tmp_dir / "fusion_policy.py")
        fusion_policy_spec_path = self._optional_relative(tmp_dir / "fusion_policy_spec.md")
        param_space_path = self._optional_relative(tmp_dir / "param_space.json")
        fusion_policy_meta_path = self._optional_relative(tmp_dir / "fusion_policy_meta.json")
        daily_portfolio_agent_prompt_path = self._optional_relative(tmp_dir / "daily_portfolio_agent_prompt.md")
        daily_decision_contract_path = self._optional_relative(tmp_dir / "daily_decision_contract.json")
        daily_override_scenarios_path = self._optional_relative(tmp_dir / "daily_override_scenarios.md")
        budget_policy_config_path = self._optional_relative(tmp_dir / "budget_policy" / "budget_policy_config.json")
        signal_strategies_dir = self._optional_relative(tmp_dir / "signal_strategies")
        fusion_diagnostics_path = self._optional_relative(tmp_dir / "evaluation" / "fusion_diagnostics.parquet")
        fusion_asset_diagnostics_path = self._optional_relative(tmp_dir / "evaluation" / "fusion_asset_diagnostics.parquet")
        fusion_diagnostics_json_path = self._optional_relative(tmp_dir / "evaluation" / "fusion_diagnostics.json")
        fusion_diagnostics_report_path = self._optional_relative(tmp_dir / "evaluation" / "fusion_diagnostics.md")

        report_text = self._format_portfolio_final_selection_report(
            state=state,
            version=version,
            version_id=version_id,
            reason=reason,
            selected_at=now,
            report_path=report_path,
            metrics_path=metrics_path,
            final_manifest_path=self._relative(final_dir / "final_manifest.json"),
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
        selection_report_target.write_text(report_text, encoding="utf-8")

        final_manifest = {
            "schema_version": "0.1.0",
            "portfolio_run_id": portfolio_run_id,
            "selected_version_id": version_id,
            "selected_at": now,
            "reason": reason,
            "source_version_dir": self._relative(version_dir),
            "final_dir": self._relative(final_dir),
            "fusion_manifest_path": fusion_manifest_path,
            "fusion_policy_path": fusion_policy_path,
            "fusion_policy_spec_path": fusion_policy_spec_path,
            "param_space_path": param_space_path,
            "fusion_policy_meta_path": fusion_policy_meta_path,
            "daily_portfolio_agent_prompt_path": daily_portfolio_agent_prompt_path,
            "daily_decision_contract_path": daily_decision_contract_path,
            "daily_override_scenarios_path": daily_override_scenarios_path,
            "budget_policy_config_path": budget_policy_config_path,
            "signal_strategies_dir": signal_strategies_dir,
            "evaluation_manifest_path": self._optional_relative(tmp_dir / "evaluation" / "evaluation_manifest.json"),
            "fusion_diagnostics_path": fusion_diagnostics_path,
            "fusion_asset_diagnostics_path": fusion_asset_diagnostics_path,
            "fusion_diagnostics_json_path": fusion_diagnostics_json_path,
            "fusion_diagnostics_report_path": fusion_diagnostics_report_path,
            "daily_budget_weights_path": self._optional_relative(tmp_dir / "evaluation" / "daily_budget_weights.parquet"),
            "daily_signal_targets_path": self._optional_relative(tmp_dir / "evaluation" / "daily_signal_targets.parquet"),
            "daily_final_weights_path": self._optional_relative(tmp_dir / "evaluation" / "daily_final_weights.parquet"),
            "metrics_path": metrics_path,
            "selection_report_path": self._relative(final_dir / "portfolio_final_selection.md"),
        }
        final_manifest_path.write_text(json.dumps(final_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        archived_final_dir = None
        if final_dir.exists():
            history_dir.mkdir(parents=True, exist_ok=True)
            archive_name = f"final_{self._safe_name(str(state.get('final_selection', {}).get('version_id') or 'unknown'))}_{datetime.now(self._timezone()).strftime('%Y%m%d_%H%M%S')}"
            archived = history_dir / archive_name
            shutil.move(str(final_dir), str(archived))
            archived_final_dir = self._relative(archived)
        shutil.move(str(tmp_dir), str(final_dir))

        final_manifest["final_dir"] = self._relative(final_dir)
        final_manifest["fusion_manifest_path"] = self._optional_relative(final_dir / "fusion_manifest.json")
        final_manifest["fusion_policy_path"] = self._optional_relative(final_dir / "fusion_policy.py")
        final_manifest["fusion_policy_spec_path"] = self._optional_relative(final_dir / "fusion_policy_spec.md")
        final_manifest["param_space_path"] = self._optional_relative(final_dir / "param_space.json")
        final_manifest["fusion_policy_meta_path"] = self._optional_relative(final_dir / "fusion_policy_meta.json")
        final_manifest["daily_portfolio_agent_prompt_path"] = self._optional_relative(final_dir / "daily_portfolio_agent_prompt.md")
        final_manifest["daily_decision_contract_path"] = self._optional_relative(final_dir / "daily_decision_contract.json")
        final_manifest["daily_override_scenarios_path"] = self._optional_relative(final_dir / "daily_override_scenarios.md")
        final_manifest["budget_policy_config_path"] = self._optional_relative(final_dir / "budget_policy" / "budget_policy_config.json")
        final_manifest["signal_strategies_dir"] = self._optional_relative(final_dir / "signal_strategies")
        final_manifest["evaluation_manifest_path"] = self._optional_relative(final_dir / "evaluation" / "evaluation_manifest.json")
        final_manifest["fusion_diagnostics_path"] = self._optional_relative(final_dir / "evaluation" / "fusion_diagnostics.parquet")
        final_manifest["fusion_asset_diagnostics_path"] = self._optional_relative(final_dir / "evaluation" / "fusion_asset_diagnostics.parquet")
        final_manifest["fusion_diagnostics_json_path"] = self._optional_relative(final_dir / "evaluation" / "fusion_diagnostics.json")
        final_manifest["fusion_diagnostics_report_path"] = self._optional_relative(final_dir / "evaluation" / "fusion_diagnostics.md")
        final_manifest["daily_budget_weights_path"] = self._optional_relative(final_dir / "evaluation" / "daily_budget_weights.parquet")
        final_manifest["daily_signal_targets_path"] = self._optional_relative(final_dir / "evaluation" / "daily_signal_targets.parquet")
        final_manifest["daily_final_weights_path"] = self._optional_relative(final_dir / "evaluation" / "daily_final_weights.parquet")
        final_manifest["metrics_path"] = self._optional_relative(final_dir / "evaluation" / "backtest" / "metrics.json")
        final_manifest["selection_report_path"] = self._relative(final_dir / "portfolio_final_selection.md")
        (final_dir / "final_manifest.json").write_text(json.dumps(final_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        report_key = f"portfolio_final_selection_{self._safe_name(version_id)}"
        final_selection = {
            "status": "selected",
            "version_id": version_id,
            "reason": reason,
            "selected_at": now,
            "selection_report_path": self._relative(report_path),
            "final_dir": self._relative(final_dir),
            "final_manifest_path": self._relative(final_dir / "final_manifest.json"),
            "source_version_dir": self._relative(version_dir),
            "metrics_path": final_manifest["metrics_path"],
            "fusion_manifest_path": final_manifest["fusion_manifest_path"],
            "fusion_policy_path": final_manifest["fusion_policy_path"],
            "daily_portfolio_agent_prompt_path": final_manifest["daily_portfolio_agent_prompt_path"],
            "daily_decision_contract_path": final_manifest["daily_decision_contract_path"],
            "daily_override_scenarios_path": final_manifest["daily_override_scenarios_path"],
            "fusion_diagnostics_json_path": final_manifest["fusion_diagnostics_json_path"],
            "daily_final_weights_path": final_manifest["daily_final_weights_path"],
            "budget_policy_config_path": final_manifest["budget_policy_config_path"],
            "signal_strategies_dir": final_manifest["signal_strategies_dir"],
        }
        if archived_final_dir:
            final_selection["archived_previous_final_dir"] = archived_final_dir
        state["final_selection"] = final_selection
        state["best_version"] = version_id
        state["current_version"] = version_id
        state["status"] = "final_selected"
        state.setdefault("artifacts", {}).setdefault("final", {})["portfolio_version"] = final_selection
        state.setdefault("artifacts", {}).setdefault("run_reports", {})[report_key] = {
            "report_type": "portfolio_final_selection",
            "path": self._relative(report_path),
            "summary": reason,
            "created_at": now,
            "version_id": version_id,
            "final_manifest_path": final_selection["final_manifest_path"],
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "PortfolioRunManager",
                "event": "portfolio_final_selection_completed",
                "summary": reason or f"组合层最终版本已选择：{version_id}",
                "version_id": version_id,
                "final_manifest_path": final_selection["final_manifest_path"],
                "selection_report_path": final_selection["selection_report_path"],
            }
        )
        state["updated_at"] = now
        self.save_state(state_path_actual, state)
        return {
            "status": "success",
            "portfolio_run_state_path": self._relative(state_path_actual),
            "final_selection": final_selection,
            "final_manifest": final_manifest,
            "report_key": report_key,
        }

    def new_run_id(self, budget_state: dict[str, Any]) -> str:
        raw = str(budget_state.get("task", {}).get("pool_name") or budget_state.get("budget_run_id") or "portfolio")
        safe = self._safe_name(raw)
        return f"portfolio_{safe}_{datetime.now(self._timezone()).strftime('%Y%m%d_%H%M%S')}"

    def _build_paths(self, portfolio_run_id: str) -> PortfolioRunPaths:
        root_dir = self.config.root_dir / "artifacts" / "portfolio_runs" / portfolio_run_id
        return PortfolioRunPaths(
            portfolio_run_id=portfolio_run_id,
            root_dir=root_dir,
            state_path=root_dir / "portfolio_run_state.json",
            source_artifacts_dir=root_dir / "source_artifacts",
            budget_source_dir=root_dir / "source_artifacts" / "budget",
            signal_source_dir=root_dir / "source_artifacts" / "signals",
            data_dir=root_dir / "data",
            versions_dir=root_dir / "versions",
            reports_dir=root_dir / "reports",
            logs_dir=root_dir / "logs",
        )

    def _build_initial_state(
        self,
        *,
        paths: PortfolioRunPaths,
        budget_state: dict[str, Any],
        budget_state_path: Path,
        budget_artifacts: dict[str, Any],
        signal_artifacts: dict[str, Any],
        selected_budget: dict[str, Any],
        task_description: str | None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        symbols = [item["symbol"] for item in signal_artifacts["items"]]
        return {
            "schema_version": "0.1.0",
            "portfolio_run_id": paths.portfolio_run_id,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "task": {
                "description": task_description or f"融合预算层 {budget_state.get('budget_run_id')} 与信号层最终策略。",
                "source_budget_run_path": self._relative(budget_state_path),
                "data_range": {
                    "start": None,
                    "end": None,
                    "frequency": "1d",
                },
                "objective": {
                    "primary": "sharpe",
                    "secondary": [
                        "max_drawdown_control",
                        "walk_forward_stability",
                        "turnover_control",
                        "budget_signal_consistency",
                    ],
                },
            },
            "directories": {
                "root": self._relative(paths.root_dir),
                "source_artifacts": self._relative(paths.source_artifacts_dir),
                "budget_source": self._relative(paths.budget_source_dir),
                "signal_source": self._relative(paths.signal_source_dir),
                "data": self._relative(paths.data_dir),
                "versions": self._relative(paths.versions_dir),
                "reports": self._relative(paths.reports_dir),
                "logs": self._relative(paths.logs_dir),
            },
            "source_artifacts": {
                "budget": budget_artifacts,
                "signals": signal_artifacts,
            },
            "data": {
                "status": "pending",
                "panel_ohlcv": None,
                "returns_wide": None,
                "split_manifest": None,
                "summary": None,
            },
            "profile": {
                "status": "pending",
                "profile_dir": None,
                "portfolio_profile_path": None,
                "portfolio_profile_md_path": None,
                "budget_signal_alignment_path": None,
                "updated_at": None,
            },
            "versions": [],
            "current_version": None,
            "best_version": None,
            "final_selection": {
                "status": "pending",
                "version_id": None,
                "reason": None,
                "selected_at": None,
            },
            "events": [
                {
                    "timestamp": now,
                    "actor": "PortfolioRunManager",
                    "event": "portfolio_run_created",
                    "summary": f"创建组合层任务 {paths.portfolio_run_id}，复制 {len(symbols)} 个信号层资产源产物，尚未创建组合层版本。",
                    "source_budget_run_id": budget_state.get("budget_run_id"),
                    "budget_search_id": selected_budget.get("search_id"),
                }
            ],
        }

    def _resolve_selected_budget_policy(
        self,
        *,
        budget_state: dict[str, Any],
    ) -> dict[str, Any]:
        searches = budget_state.get("artifacts", {}).get("policies", {}).get("searches", {}) or {}
        final = budget_state.get("final_selection") or {}
        selected_search_id = final.get("search_id") or final.get("attempt_id")
        policy_path = final.get("policy_config_path") or final.get("policy_path")
        selection_report_path = final.get("selection_report_path")
        reason = final.get("reason")
        best_score = final.get("best_score")

        if selected_search_id and selected_search_id in searches:
            search = searches[selected_search_id]
            policy_path = policy_path or search.get("best_policy_config_path")
            best_score = best_score if best_score is not None else search.get("best_score")

        if not selected_search_id or not policy_path:
            raise ValueError("预算层 run 必须已经完成 final_selection，才能创建组合层 run。")

        actual_policy_path = self._resolve_path(policy_path)
        if not actual_policy_path.exists():
            raise FileNotFoundError(f"最终预算策略文件不存在：{actual_policy_path}")

        policy_config = self._read_json(actual_policy_path)
        return {
            "search_id": selected_search_id,
            "policy_name": policy_config.get("policy_name") or policy_config.get("policy_id") or selected_search_id,
            "policy_config_path": self._relative(actual_policy_path),
            "selection_report_path": selection_report_path,
            "reason": reason,
            "best_score": best_score,
            "policy_config": policy_config,
        }

    def _copy_budget_artifacts(
        self,
        *,
        budget_state_path: Path,
        selected_budget: dict[str, Any],
        paths: PortfolioRunPaths,
    ) -> dict[str, Any]:
        copied: dict[str, str | None] = {}
        copied["budget_run_state_path"] = self._copy_file(
            budget_state_path,
            paths.budget_source_dir / "budget_run_state.json",
        )
        policy_source = self._resolve_path(str(selected_budget["policy_config_path"]))
        policy_dir = policy_source.parent
        policy_target_dir = paths.budget_source_dir / "final_budget_policy"
        if policy_dir.exists():
            shutil.copytree(policy_dir, policy_target_dir, dirs_exist_ok=True)
        copied["final_budget_policy_config_path"] = self._copy_file(
            policy_source,
            paths.budget_source_dir / "final_budget_policy_config.json",
        )
        for optional_key, target_name in [
            ("selection_report_path", "budget_final_selection.md"),
        ]:
            source_value = selected_budget.get(optional_key)
            if source_value:
                source = self._resolve_path(str(source_value))
                copied[optional_key] = self._copy_file(source, paths.budget_source_dir / target_name) if source.exists() else None
            else:
                copied[optional_key] = None
        memory_source = budget_state_path.parent / "reports" / "budget_agent_memory.md"
        copied["budget_agent_memory_path"] = (
            self._copy_file(memory_source, paths.budget_source_dir / "budget_agent_memory.md")
            if memory_source.exists()
            else None
        )
        return {
            "status": "success",
            "source_budget_run_state_path": self._relative(budget_state_path),
            "search_id": selected_budget["search_id"],
            "policy_name": selected_budget.get("policy_name"),
            "best_score": selected_budget.get("best_score"),
            **copied,
        }

    def _copy_signal_artifacts(
        self,
        *,
        budget_state: dict[str, Any],
        budget_state_path: Path,
        paths: PortfolioRunPaths,
    ) -> dict[str, Any]:
        manifest_path = self._resolve_signal_manifest_path(budget_state, budget_state_path)
        manifest = self._read_json(manifest_path)
        records = manifest.get("records") or []
        if not records:
            raise ValueError(f"未在信号层 manifest 中找到 records：{manifest_path}")

        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        for record in records:
            symbol = str(record.get("symbol") or "").strip()
            if not symbol:
                warnings.append(f"跳过缺少 symbol 的信号层记录：{record}")
                continue
            symbol_dir = paths.signal_source_dir / self._safe_name(symbol)
            symbol_dir.mkdir(parents=True, exist_ok=True)
            copied_files: dict[str, str | None] = {}
            source_files = record.get("copied_files") if isinstance(record.get("copied_files"), dict) else {}
            file_map = {
                "run_state_path": "run_state.json",
                "selected_strategy_path": "strategy.py",
                "strategy_spec_path": "strategy_spec.md",
                "param_space_path": "param_space.json",
                "strategy_meta_path": "strategy_meta.json",
                "signal_agent_memory_path": "signal_agent_memory.md",
                "selected_metrics_path": "metrics.json",
            }
            missing: list[str] = []
            for key, target_name in file_map.items():
                raw_source = source_files.get(key) or record.get(key)
                if not raw_source:
                    copied_files[key] = None
                    missing.append(key)
                    continue
                source = self._resolve_path(str(raw_source))
                if not source.exists():
                    copied_files[key] = None
                    missing.append(key)
                    continue
                copied_files[key] = self._copy_file(source, symbol_dir / target_name)
            if missing:
                warnings.append(f"{symbol} 缺少信号层小文件：{', '.join(missing)}")
            items.append(
                {
                    "symbol": symbol,
                    "run_id": record.get("run_id"),
                    "asset_type": record.get("asset_type"),
                    "data_range": record.get("data_range"),
                    "primary_dataset": record.get("primary_dataset"),
                    "selected_attempt_id": record.get("selected_attempt_id"),
                    "score": record.get("score"),
                    "artifact_dir": self._relative(symbol_dir),
                    "copied_files": copied_files,
                    "missing": missing,
                }
            )
        if not items:
            raise ValueError("没有成功复制任何信号层策略文件。")
        signal_manifest_path = paths.signal_source_dir / "portfolio_signal_artifacts_manifest.json"
        payload = {
            "status": "success" if not warnings else "partial_success",
            "source_manifest_path": self._relative(manifest_path),
            "count": len(items),
            "symbols": [item["symbol"] for item in items],
            "items": items,
            "warnings": warnings,
        }
        signal_manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        payload["manifest_path"] = self._relative(signal_manifest_path)
        return payload

    def _resolve_budget_state_path(self, source_budget_run_path: str | Path) -> Path:
        path = self._resolve_path(source_budget_run_path)
        if path.is_dir():
            path = path / "budget_run_state.json"
        if not path.exists():
            raise FileNotFoundError(f"budget_run_state.json 不存在：{path}")
        return path

    def _resolve_signal_manifest_path(self, budget_state: dict[str, Any], budget_state_path: Path) -> Path:
        raw = budget_state.get("signal_artifacts", {}).get("manifest_path")
        if raw:
            path = self._resolve_path(raw)
            if path.exists():
                return path
        fallback = budget_state_path.parent / "signal_artifacts" / "signal_artifacts_manifest.json"
        if fallback.exists():
            return fallback
        raise FileNotFoundError(f"signal_artifacts_manifest.json 不存在：{fallback}")

    def _copy_file(self, source: str | Path, target: str | Path) -> str:
        source_path = self._resolve_path(source)
        target_path = self._resolve_path(target)
        if not source_path.exists():
            raise FileNotFoundError(f"源文件不存在：{source_path}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        return self._relative(target_path)

    def _signal_strategy_params_from_snapshot(self, strategy_dir: Path) -> dict[str, Any]:
        params = self._params_from_param_space(strategy_dir / "param_space.json")
        run_state_path = strategy_dir / "run_state.json"
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

    def _init_version_budget_snapshot(self, *, state: dict[str, Any], budget_dir: Path) -> dict[str, Any]:
        budget_source = state.get("source_artifacts", {}).get("budget", {})
        budget_source_dir_raw = state.get("directories", {}).get("budget_source")
        budget_source_dir = self._resolve_path(budget_source_dir_raw) if budget_source_dir_raw else None
        final_policy_dir = budget_source_dir / "final_budget_policy" if budget_source_dir else None
        if final_policy_dir and final_policy_dir.exists():
            shutil.copytree(final_policy_dir, budget_dir, dirs_exist_ok=True)

        policy_config_raw = budget_source.get("final_budget_policy_config_path")
        if policy_config_raw:
            policy_config_source = self._resolve_path(policy_config_raw)
            if policy_config_source.exists():
                self._copy_file(policy_config_source, budget_dir / "budget_policy_config.json")

        required = budget_dir / "budget_policy_config.json"
        if not required.exists():
            raise FileNotFoundError(f"无法初始化预算层快照，缺少 budget_policy_config.json：{required}")
        return {
            "budget_policy_dir": self._relative(budget_dir),
            "budget_policy_config_path": self._relative(required),
        }

    def _init_version_signal_snapshots(self, *, state: dict[str, Any], signals_dir: Path) -> list[dict[str, Any]]:
        signal_source = state.get("source_artifacts", {}).get("signals", {})
        items = signal_source.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("portfolio_run_state.json 缺少 source_artifacts.signals.items，无法初始化信号层快照。")

        signals: list[dict[str, Any]] = []
        missing_details: dict[str, list[str]] = {}
        for item in items:
            symbol = str(item.get("symbol") or "").strip()
            if not symbol:
                continue
            symbol_dir = signals_dir / self._safe_name(symbol)
            symbol_dir.mkdir(parents=True, exist_ok=True)

            source_dir_raw = item.get("artifact_dir")
            source_dir = self._resolve_path(source_dir_raw) if source_dir_raw else None
            if source_dir and source_dir.exists():
                shutil.copytree(source_dir, symbol_dir, dirs_exist_ok=True)
            else:
                copied_files = item.get("copied_files") if isinstance(item.get("copied_files"), dict) else {}
                for source_key, target_name in [
                    ("run_state_path", "run_state.json"),
                    ("selected_strategy_path", "strategy.py"),
                    ("strategy_spec_path", "strategy_spec.md"),
                    ("param_space_path", "param_space.json"),
                    ("strategy_meta_path", "strategy_meta.json"),
                    ("signal_agent_memory_path", "signal_agent_memory.md"),
                    ("selected_metrics_path", "metrics.json"),
                ]:
                    raw = copied_files.get(source_key)
                    if raw and self._resolve_path(raw).exists():
                        self._copy_file(raw, symbol_dir / target_name)

            strategy_params_path = symbol_dir / "strategy_params.json"
            if not strategy_params_path.exists():
                strategy_params_path.write_text(
                    json.dumps(self._signal_strategy_params_from_snapshot(symbol_dir), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )

            required_paths = {
                "strategy.py": symbol_dir / "strategy.py",
                "param_space.json": symbol_dir / "param_space.json",
                "strategy_meta.json": symbol_dir / "strategy_meta.json",
                "strategy_params.json": strategy_params_path,
            }
            missing = [name for name, path in required_paths.items() if not path.exists()]
            if missing:
                missing_details[symbol] = missing
                continue
            signals.append(
                {
                    "symbol": symbol,
                    "asset_type": item.get("asset_type"),
                    "run_id": item.get("run_id"),
                    "selected_attempt_id": item.get("selected_attempt_id"),
                    "signal_strategy_dir": self._relative(symbol_dir),
                    "strategy_path": self._relative(symbol_dir / "strategy.py"),
                    "strategy_spec_path": self._optional_relative(symbol_dir / "strategy_spec.md"),
                    "param_space_path": self._relative(symbol_dir / "param_space.json"),
                    "strategy_meta_path": self._relative(symbol_dir / "strategy_meta.json"),
                    "strategy_params_path": self._relative(strategy_params_path),
                }
            )

        if missing_details:
            raise FileNotFoundError(f"初始化信号层快照失败，存在缺失文件：{missing_details}")
        if not signals:
            raise ValueError("没有成功初始化任何信号层快照。")
        return signals

    def _default_fusion_policy_py(self, *, policy_name: str) -> str:
        return f'''from __future__ import annotations

from typing import Any

import pandas as pd


class PortfolioFusionPolicy:
    """系统初始化的预算层直用基线。

    PortfolioAgent 应阅读组合层画像和策略指南后改写本脚本。
    """

    def __init__(self, params: dict[str, Any] | None = None):
        defaults = {{
            "max_gross": 1.0,
            "max_weight": 0.30,
            "rebalance_speed": 1.0,
            "max_turnover_per_day": 0.60,
        }}
        self.params = {{**defaults, **(params or {{}})}}
        self.policy_name = "{policy_name}"

    def generate_weights(
        self,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        returns: pd.DataFrame,
        signal_profile: dict[str, Any] | None = None,
        market_context: dict[str, Any] | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        index = budget_weights.index.intersection(signal_targets.index).intersection(returns.index)
        columns = sorted(set(budget_weights.columns) & set(signal_targets.columns) & set(returns.columns))
        budget = budget_weights.loc[index, columns].fillna(0.0).clip(lower=0.0)
        raw = budget.clip(upper=float(self.params["max_weight"]))
        raw = self._scale_to_gross(raw, float(self.params["max_gross"]))
        weights = self._smooth_and_limit_turnover(raw)
        diagnostics = pd.DataFrame(index=weights.index)
        diagnostics["gross_exposure"] = weights.sum(axis=1)
        diagnostics["cash_weight"] = 1.0 - diagnostics["gross_exposure"]
        diagnostics["turnover"] = weights.diff().abs().sum(axis=1).fillna(weights.abs().sum(axis=1))
        diagnostics["budget_gross"] = budget.sum(axis=1)
        diagnostics["signal_mean"] = signal_targets.loc[index, columns].fillna(0.0).mean(axis=1)
        diagnostics["signal_breadth"] = signal_targets.loc[index, columns].fillna(0.0).gt(0.3).mean(axis=1)
        return weights, diagnostics

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

    @staticmethod
    def _scale_to_gross(frame: pd.DataFrame, max_gross: float) -> pd.DataFrame:
        gross = frame.sum(axis=1)
        scale = (max_gross / gross.mask(gross == 0.0)).clip(upper=1.0).fillna(1.0)
        return frame.mul(scale, axis=0)
'''

    def _default_fusion_param_space(self) -> dict[str, Any]:
        return {
            "max_gross": {"type": "float", "values": [0.8, 0.9, 1.0], "default": 1.0},
            "max_weight": {"type": "float", "values": [0.2, 0.25, 0.3], "default": 0.3},
            "rebalance_speed": {"type": "float", "values": [0.5, 0.75, 1.0], "default": 1.0},
            "max_turnover_per_day": {"type": "float", "values": [0.3, 0.45, 0.6], "default": 0.6},
        }

    def _default_fusion_spec(
        self,
        *,
        state: dict[str, Any],
        version_id: str,
        policy_name: str,
        summary: str | None,
    ) -> str:
        lines = [
            f"# 组合层融合策略说明：{policy_name}",
            "",
            f"- version_id: {version_id}",
            f"- portfolio_run_id: {state.get('portfolio_run_id')}",
            "- policy_mode: python_script",
            "- fusion_type: python_policy",
            "- primary_objective: sharpe",
            "",
            "## 初始化说明",
            "",
            summary or "本版本由 init-fusion-version 自动初始化，上游预算层和信号层文件均为冻结快照。",
            "",
            "PortfolioAgent 应在阅读 portfolio-profile 和 fusion_policy_library.md 后，修改 fusion_policy.py、param_space.json、fusion_policy_meta.json 和本说明文件。",
            "",
            "## 默认逻辑",
            "",
            "默认策略使用预算权重、信号强度和波动惩罚生成机会分，再用 score_normalize 分配最终仓位。",
            "该默认逻辑只作为可运行起点，不代表最终策略。",
            "",
        ]
        return "\n".join(lines)

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

    def _find_version(self, state: dict[str, Any], version_id: str) -> dict[str, Any]:
        version = next((item for item in state.get("versions", []) if item.get("version_id") == version_id), None)
        if version is None:
            raise ValueError(f"portfolio_run_state.json 中不存在 version_id={version_id}。")
        return version

    def _validate_final_selection_candidate(self, *, version: dict[str, Any], version_dir: Path, version_id: str) -> None:
        required_paths = {
            "fusion_manifest.json": version_dir / "fusion_manifest.json",
            "fusion_policy.py": version_dir / "fusion_policy.py",
            "param_space.json": version_dir / "param_space.json",
            "fusion_policy_spec.md": version_dir / "fusion_policy_spec.md",
            "fusion_policy_meta.json": version_dir / "fusion_policy_meta.json",
            "evaluation/evaluation_manifest.json": version_dir / "evaluation" / "evaluation_manifest.json",
            "evaluation/backtest/metrics.json": version_dir / "evaluation" / "backtest" / "metrics.json",
            "evaluation/daily_final_weights.parquet": version_dir / "evaluation" / "daily_final_weights.parquet",
            "evaluation/fusion_diagnostics.json": version_dir / "evaluation" / "fusion_diagnostics.json",
            "budget_policy/budget_policy_config.json": version_dir / "budget_policy" / "budget_policy_config.json",
            "signal_strategies": version_dir / "signal_strategies",
        }
        missing = [name for name, path in required_paths.items() if not path.exists()]
        if missing:
            raise FileNotFoundError(f"组合层版本 {version_id} 尚不满足最终选择要求，缺少：{missing}")
        if version.get("status") != "evaluated" and not version.get("evaluation"):
            raise ValueError(f"组合层版本 {version_id} 尚未完成 portfolio-evaluation，不能作为最终版本。")

    def _copy_final_snapshot(self, *, version_dir: Path, final_dir: Path) -> None:
        copy_items = [
            "fusion_manifest.json",
            "fusion_policy.py",
            "fusion_policy_spec.md",
            "param_space.json",
            "fusion_policy_meta.json",
            "daily_portfolio_agent_prompt.md",
            "daily_decision_contract.json",
            "daily_override_scenarios.md",
            "budget_policy",
            "signal_strategies",
            "evaluation",
        ]
        for item in copy_items:
            source = version_dir / item
            if not source.exists():
                continue
            target = final_dir / item
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    def _resolve_final_selection_report_path(
        self,
        *,
        state: dict[str, Any],
        version_id: str,
    ) -> Path:
        reports_dir = state.get("directories", {}).get("reports")
        base_dir = self._resolve_path(reports_dir) if reports_dir else self.config.root_dir / "artifacts" / "portfolio_runs" / str(state.get("portfolio_run_id")) / "reports"
        timestamp = datetime.now(self._timezone()).strftime("%Y%m%d_%H%M%S")
        return base_dir / f"portfolio_final_selection_{self._safe_name(version_id)}_{timestamp}.md"

    def _format_portfolio_final_selection_report(
        self,
        *,
        state: dict[str, Any],
        version: dict[str, Any],
        version_id: str,
        reason: str,
        selected_at: str,
        report_path: Path,
        metrics_path: str | None,
        final_manifest_path: str,
    ) -> str:
        metrics = self._read_json_if_exists(metrics_path)
        lines = [
            "# 组合层最终选择",
            "",
            f"- portfolio_run_id: {state.get('portfolio_run_id')}",
            f"- selected_at: {selected_at}",
            f"- selected_version_id: {version_id}",
            f"- version_status: {version.get('status')}",
            f"- source_version_id: {version.get('source_version_id')}",
            f"- report_path: {self._relative(report_path)}",
            f"- final_manifest_path: {final_manifest_path}",
            "",
            "## 选择理由",
            "",
            reason.strip() or "未填写选择理由。",
            "",
            "## 关键指标",
            "",
        ]
        for key in [
            "total_return",
            "annual_return",
            "sharpe",
            "max_drawdown",
            "benchmark_total_return",
            "excess_total_return",
            "average_gross_exposure",
            "average_holding_count",
            "total_turnover",
        ]:
            lines.append(f"- {key}: {metrics.get(key)}")
        lines.extend(
            [
                "## 最终目录",
                "",
                "- `final/` 永远指向当前最终版本。",
                "- `final/final_manifest.json` 是后续组合模拟、提交、实盘前验证的统一入口。",
                "- 如果再次执行 final-select，旧 `final/` 会自动归档到 `final_history/`。",
                "",
                "## 检查结论",
                "",
                "- 已确认 selected version_id 存在于 portfolio_run_state.json。",
                "- 已复制版本文件到最终目录。",
                "- 已登记 final_selection、artifacts.final、artifacts.run_reports 和 events。",
            ]
        )
        return "\n".join(lines) + "\n"

    def _read_json_if_exists(self, path: str | Path | None) -> dict[str, Any]:
        if not path:
            return {}
        actual = self._resolve_path(path)
        if not actual.exists():
            return {}
        try:
            return json.loads(actual.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _optional_relative(self, path: str | Path) -> str | None:
        actual = self._resolve_path(path)
        return self._relative(actual) if actual.exists() else None

    def _read_json(self, path: str | Path) -> dict[str, Any]:
        actual = self._resolve_path(path)
        return json.loads(actual.read_text(encoding="utf-8-sig"))

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: str | Path) -> str:
        value = self._resolve_path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)

    def _safe_name(self, value: str) -> str:
        text = str(value).strip().replace(".", "_")
        text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text or "item"

    def _timezone(self) -> ZoneInfo:
        return ZoneInfo(self.config.project.default_timezone)

    def _now_iso(self) -> str:
        return datetime.now(self._timezone()).isoformat()
