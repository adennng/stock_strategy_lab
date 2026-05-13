from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_attempt_summary import BudgetAttemptSummaryRequest, BudgetAttemptSummaryService
from strategy_lab.services.budget_parameter_search import BudgetParameterSearchRequest, BudgetParameterSearchService
from strategy_lab.services.budget_run import BudgetRunManager
from strategy_lab.services.budget_stage_attribution import BudgetStageAttributionRequest, BudgetStageAttributionService


class BudgetPolicyEvaluationRequest(BaseModel):
    budget_run_state_path: Path
    policy_dir: Path | None = None
    policy_config_path: Path | None = None
    param_space_path: Path | None = None
    policy_spec_path: Path | None = None
    policy_meta_path: Path | None = None
    policy_name: str | None = None
    search_id: str | None = None
    output_dir: Path | None = None
    data_split_manifest_path: Path | None = None
    search_method: str = "ga"
    max_candidates: int = 30
    population_size: int = 10
    generations: int = 5
    mutation_rate: float = 0.20
    ga_patience: int = 3
    min_improvement: float = 1e-6
    max_workers: int = 1
    cache_enabled: bool = True
    random_seed: int = 42
    benchmark: str | None = None
    generate_chart: bool = False
    stage_chart: bool = True
    update_run_state: bool = True

    @model_validator(mode="after")
    def _validate_policy_locator(self) -> "BudgetPolicyEvaluationRequest":
        if not self.policy_dir and not self.policy_config_path:
            raise ValueError("必须提供 policy_dir 或 policy_config_path。")
        return self


class BudgetPolicyEvaluationResult(BaseModel):
    budget_run_state_path: Path
    search_id: str
    policy_name: str | None = None
    search_result_path: Path
    best_policy_config_path: Path
    attempt_summary_path: Path
    attempt_summary_md_path: Path
    stage_attribution_path: Path
    stage_attribution_md_path: Path
    stage_attribution_csv_path: Path
    stage_attribution_chart_path: Path | None = None
    best_score: float
    best_params: dict[str, Any] = Field(default_factory=dict)
    summary_row: dict[str, Any] = Field(default_factory=dict)


class BudgetPolicyEvaluationService:
    """预算层单策略一键评估服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetPolicyEvaluationRequest) -> BudgetPolicyEvaluationResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        policy = self._normalize_policy(request)
        search_id = request.search_id or self._make_search_id(policy["policy_name"])

        search = BudgetParameterSearchService(config=self.config).run(
            BudgetParameterSearchRequest(
                budget_run_state_path=state_path,
                policy_config_path=policy["policy_config_path"],
                param_space_path=policy["param_space_path"],
                output_dir=request.output_dir,
                search_id=search_id,
                data_split_manifest_path=request.data_split_manifest_path,
                search_method=request.search_method,
                max_candidates=request.max_candidates,
                population_size=request.population_size,
                generations=request.generations,
                mutation_rate=request.mutation_rate,
                ga_patience=request.ga_patience,
                min_improvement=request.min_improvement,
                max_workers=request.max_workers,
                cache_enabled=request.cache_enabled,
                random_seed=request.random_seed,
                benchmark=request.benchmark,
                update_run_state=False,
                generate_chart=request.generate_chart,
            )
        )
        attempt_summary = BudgetAttemptSummaryService(config=self.config).run(
            BudgetAttemptSummaryRequest(
                budget_run_state_path=state_path,
                search_result_path=search.search_result_path,
                update_run_state=False,
            )
        )
        stage = BudgetStageAttributionService(config=self.config).run(
            BudgetStageAttributionRequest(
                budget_run_state_path=state_path,
                search_result_path=search.search_result_path,
                generate_chart=request.stage_chart,
                update_run_state=False,
            )
        )
        row = self._summary_row(
            policy=policy,
            search=search,
            attempt_summary=attempt_summary.summary,
            attempt_summary_path=attempt_summary.summary_json_path,
            attempt_summary_md_path=attempt_summary.summary_md_path,
            stage_summary=stage.summary,
            stage_json_path=stage.json_path,
            stage_md_path=stage.markdown_path,
            stage_csv_path=stage.csv_path,
            stage_chart_path=stage.chart_path,
        )
        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                row=row,
            )
        return BudgetPolicyEvaluationResult(
            budget_run_state_path=state_path,
            search_id=search.search_id,
            policy_name=policy["policy_name"],
            search_result_path=search.search_result_path,
            best_policy_config_path=search.best_policy_config_path,
            attempt_summary_path=attempt_summary.summary_json_path,
            attempt_summary_md_path=attempt_summary.summary_md_path,
            stage_attribution_path=stage.json_path,
            stage_attribution_md_path=stage.markdown_path,
            stage_attribution_csv_path=stage.csv_path,
            stage_attribution_chart_path=stage.chart_path,
            best_score=search.best_score,
            best_params=search.best_params,
            summary_row=row,
        )

    def _normalize_policy(self, request: BudgetPolicyEvaluationRequest) -> dict[str, Any]:
        policy_dir = self._resolve_path(request.policy_dir) if request.policy_dir else None
        policy_config_path = request.policy_config_path
        param_space_path = request.param_space_path
        policy_spec_path = request.policy_spec_path
        policy_meta_path = request.policy_meta_path
        if policy_dir:
            policy_config_path = policy_config_path or policy_dir / "budget_policy_config.json"
            param_space_path = param_space_path or policy_dir / "param_space.json"
            policy_spec_path = policy_spec_path or policy_dir / "budget_policy_spec.md"
            policy_meta_path = policy_meta_path or policy_dir / "budget_policy_meta.json"
        if not policy_config_path or not param_space_path:
            raise ValueError("必须提供 budget_policy_config.json 和 param_space.json。")
        policy_config = self._resolve_path(policy_config_path)
        param_space = self._resolve_path(param_space_path)
        if not policy_config.exists():
            raise FileNotFoundError(f"budget_policy_config.json 不存在：{policy_config}")
        if not param_space.exists():
            raise FileNotFoundError(f"param_space.json 不存在：{param_space}")
        spec = self._resolve_optional_existing(policy_spec_path)
        meta = self._resolve_optional_existing(policy_meta_path)
        policy_name = request.policy_name
        if not policy_name:
            try:
                payload = json.loads(policy_config.read_text(encoding="utf-8"))
                policy_name = str(payload.get("policy_name") or payload.get("policy_id") or policy_config.parent.name)
            except Exception:
                policy_name = policy_config.parent.name
        return {
            "policy_dir": policy_dir,
            "policy_config_path": policy_config,
            "param_space_path": param_space,
            "policy_spec_path": spec,
            "policy_meta_path": meta,
            "policy_name": policy_name,
        }

    def _summary_row(
        self,
        *,
        policy: dict[str, Any],
        search: Any,
        attempt_summary: dict[str, Any],
        attempt_summary_path: Path,
        attempt_summary_md_path: Path,
        stage_summary: dict[str, Any],
        stage_json_path: Path,
        stage_md_path: Path,
        stage_csv_path: Path,
        stage_chart_path: Path | None,
    ) -> dict[str, Any]:
        full_metrics = attempt_summary.get("backtests", {}).get("full", {}).get("metrics", {}) or {}
        validation_metrics = attempt_summary.get("backtests", {}).get("validation", {}).get("metrics", {}) or {}
        walk_forward = attempt_summary.get("optimization", {}).get("walk_forward", {}) or {}
        best_stage = stage_summary.get("best_excess_stage") or {}
        worst_stage = stage_summary.get("worst_excess_stage") or {}
        return {
            "status": "success",
            "search_id": search.search_id,
            "policy_name": policy.get("policy_name"),
            "policy_dir": str(self._relative(policy.get("policy_dir"))) if policy.get("policy_dir") else None,
            "policy_config_path": str(self._relative(policy["policy_config_path"])),
            "param_space_path": str(self._relative(policy["param_space_path"])),
            "policy_spec_path": str(self._relative(policy.get("policy_spec_path"))) if policy.get("policy_spec_path") else None,
            "policy_meta_path": str(self._relative(policy.get("policy_meta_path"))) if policy.get("policy_meta_path") else None,
            "best_score": search.best_score,
            "best_params": search.best_params,
            "search_result_path": str(self._relative(search.search_result_path)),
            "best_policy_config_path": str(self._relative(search.best_policy_config_path)),
            "population_summary_path": str(self._relative(search.population_summary_path)),
            "walk_forward_summary_path": str(self._relative(search.walk_forward_summary_path)) if search.walk_forward_summary_path else None,
            "attempt_summary_path": str(self._relative(attempt_summary_path)),
            "attempt_summary_md_path": str(self._relative(attempt_summary_md_path)),
            "stage_attribution_path": str(self._relative(stage_json_path)),
            "stage_attribution_md_path": str(self._relative(stage_md_path)),
            "stage_attribution_csv_path": str(self._relative(stage_csv_path)),
            "stage_attribution_chart_path": str(self._relative(stage_chart_path)) if stage_chart_path else None,
            "full_total_return": full_metrics.get("total_return"),
            "full_annual_return": full_metrics.get("annual_return"),
            "full_sharpe": full_metrics.get("sharpe"),
            "full_max_drawdown": full_metrics.get("max_drawdown"),
            "full_excess_total_return": full_metrics.get("excess_total_return"),
            "full_average_turnover": full_metrics.get("average_turnover"),
            "full_average_gross_exposure": full_metrics.get("average_gross_exposure"),
            "full_average_holding_count": full_metrics.get("average_holding_count"),
            "validation_total_return": validation_metrics.get("total_return"),
            "validation_sharpe": validation_metrics.get("sharpe"),
            "validation_max_drawdown": validation_metrics.get("max_drawdown"),
            "validation_excess_total_return": validation_metrics.get("excess_total_return"),
            "walk_forward_mean_score": walk_forward.get("mean_score"),
            "walk_forward_std_score": walk_forward.get("std_score"),
            "walk_forward_min_score": walk_forward.get("min_score"),
            "walk_forward_max_score": walk_forward.get("max_score"),
            "stage_count": stage_summary.get("stage_count"),
            "best_excess_stage": best_stage.get("stage_id"),
            "best_excess_stage_return": best_stage.get("excess_return"),
            "worst_excess_stage": worst_stage.get("stage_id"),
            "worst_excess_stage_return": worst_stage.get("excess_return"),
        }

    def _update_run_state(self, *, state_path: Path, state: dict[str, Any], row: dict[str, Any]) -> None:
        search_id = row["search_id"]
        now = datetime.now().isoformat()
        state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("searches", {})[search_id] = {
            "search_result_path": row.get("search_result_path"),
            "best_policy_config_path": row.get("best_policy_config_path"),
            "population_summary_path": row.get("population_summary_path"),
            "best_score": row.get("best_score"),
            "best_params": row.get("best_params"),
            "attempt_summary_path": row.get("attempt_summary_path"),
            "attempt_summary_md_path": row.get("attempt_summary_md_path"),
            "stage_attribution_path": row.get("stage_attribution_path"),
            "stage_attribution_csv_path": row.get("stage_attribution_csv_path"),
            "stage_attribution_md_path": row.get("stage_attribution_md_path"),
            "stage_attribution_chart_path": row.get("stage_attribution_chart_path"),
            "summary": {
                "full_total_return": row.get("full_total_return"),
                "full_sharpe": row.get("full_sharpe"),
                "validation_total_return": row.get("validation_total_return"),
                "validation_sharpe": row.get("validation_sharpe"),
                "walk_forward_mean_score": row.get("walk_forward_mean_score"),
            },
        }
        state.setdefault("strategy_search", {})["current_iteration"] = int(state.get("strategy_search", {}).get("current_iteration") or 0) + 1
        state.setdefault("strategy_search", {}).setdefault("attempt_ids", []).append(search_id)
        current_best = state.setdefault("strategy_search", {}).get("best_score")
        if current_best is None or self._number(row.get("best_score")) > self._number(current_best):
            state["strategy_search"]["best_score"] = self._number(row.get("best_score"))
            state["strategy_search"]["best_attempt_id"] = search_id
        state["strategy_search"]["status"] = "running"
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetPolicyEvaluationService",
                "event": "budget_policy_evaluation_completed",
                "summary": f"预算层策略一键评估完成：{search_id}，最佳得分 {self._number(row.get('best_score')):.6f}。",
                "search_id": search_id,
                "search_result_path": row.get("search_result_path"),
                "attempt_summary_path": row.get("attempt_summary_path"),
                "stage_attribution_path": row.get("stage_attribution_path"),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _make_search_id(self, policy_name: str | None) -> str:
        safe = self._safe_name(policy_name or "budget_policy")
        return f"{safe}_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def _safe_name(self, value: str) -> str:
        text = str(value).strip().replace(".", "_")
        text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_").lower()
        return text or "budget_policy"

    def _resolve_optional_existing(self, path: str | Path | None) -> Path | None:
        if not path:
            return None
        resolved = self._resolve_path(path)
        return resolved if resolved.exists() else None

    def _number(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: Path | None) -> str:
        if path is None:
            return ""
        try:
            return str(path.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(path)
