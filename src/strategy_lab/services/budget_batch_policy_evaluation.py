from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_policy_evaluation import BudgetPolicyEvaluationRequest, BudgetPolicyEvaluationService
from strategy_lab.services.budget_run import BudgetRunManager


class BudgetPolicyCandidate(BaseModel):
    policy_dir: Path | None = None
    policy_config_path: Path | None = None
    param_space_path: Path | None = None
    policy_spec_path: Path | None = None
    policy_meta_path: Path | None = None
    policy_name: str | None = None
    search_id: str | None = None


class BudgetBatchPolicyEvaluationRequest(BaseModel):
    budget_run_state_path: Path
    policies_dir: Path | None = None
    policy_manifest_path: Path | None = None
    policies: list[BudgetPolicyCandidate] = Field(default_factory=list)
    batch_id: str | None = None
    search_prefix: str = "budget_policy"
    data_split_manifest_path: Path | None = None
    search_method: str = "ga"
    max_candidates: int = 30
    population_size: int = 10
    generations: int = 5
    mutation_rate: float = 0.20
    ga_patience: int = 3
    min_improvement: float = 1e-6
    max_workers: int = 1
    batch_workers: int = 1
    cache_enabled: bool = True
    random_seed: int = 42
    benchmark: str | None = None
    generate_chart: bool = False
    stage_chart: bool = True
    output_dir: Path | None = None
    update_run_state: bool = True


class BudgetBatchPolicyEvaluationResult(BaseModel):
    budget_run_state_path: Path
    batch_id: str
    output_dir: Path
    summary_json_path: Path
    summary_md_path: Path
    summary_csv_path: Path
    status: str
    attempted_count: int
    success_count: int
    failed_count: int
    results: list[dict[str, Any]] = Field(default_factory=list)


class BudgetBatchPolicyEvaluationService:
    """预算层多策略批量评估服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetBatchPolicyEvaluationRequest) -> BudgetBatchPolicyEvaluationResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        policies = self._collect_policies(request)
        if not policies:
            raise ValueError("没有找到可评估的预算策略目录或策略配置。")
        batch_id = request.batch_id or self._make_batch_id()
        output_dir = self._resolve_output_dir(request.output_dir, state=state, batch_id=batch_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        assigned = self._assign_search_ids(policies=policies, batch_id=batch_id, search_prefix=request.search_prefix)

        if request.batch_workers <= 1 or len(assigned) <= 1:
            results = [self._evaluate_one(policy=policy, request=request, index=index) for index, policy in enumerate(assigned, start=1)]
        else:
            by_search_id: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=max(1, request.batch_workers)) as executor:
                futures = {
                    executor.submit(self._evaluate_one, policy=policy, request=request, index=index): str(policy.search_id)
                    for index, policy in enumerate(assigned, start=1)
                }
                for future in as_completed(futures):
                    by_search_id[futures[future]] = future.result()
            results = [by_search_id[str(policy.search_id)] for policy in assigned if str(policy.search_id) in by_search_id]

        success_count = sum(1 for item in results if item.get("status") == "success")
        failed_count = len(results) - success_count
        status = "success" if success_count == len(results) else "failed" if success_count == 0 else "partial_success"
        ranked = self._rank_results(results)
        summary = {
            "schema_version": "0.1.0",
            "created_at": datetime.now().isoformat(),
            "budget_run_id": state.get("budget_run_id"),
            "budget_run_state_path": str(self._relative(state_path)),
            "batch_id": batch_id,
            "status": status,
            "attempted_count": len(results),
            "success_count": success_count,
            "failed_count": failed_count,
            "best_search_id": ranked[0].get("search_id") if ranked else None,
            "best_policy_name": ranked[0].get("policy_name") if ranked else None,
            "results": ranked,
        }
        summary_json_path = output_dir / "batch_policy_evaluation_summary.json"
        summary_md_path = output_dir / "batch_policy_evaluation_summary.md"
        summary_csv_path = output_dir / "batch_policy_evaluation_summary.csv"
        summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        pd.DataFrame(ranked).to_csv(summary_csv_path, index=False, encoding="utf-8-sig")
        summary_md_path.write_text(self._format_markdown(summary), encoding="utf-8")

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                batch_id=batch_id,
                summary=summary,
                summary_json_path=summary_json_path,
                summary_md_path=summary_md_path,
                summary_csv_path=summary_csv_path,
            )

        return BudgetBatchPolicyEvaluationResult(
            budget_run_state_path=state_path,
            batch_id=batch_id,
            output_dir=output_dir,
            summary_json_path=summary_json_path,
            summary_md_path=summary_md_path,
            summary_csv_path=summary_csv_path,
            status=status,
            attempted_count=len(results),
            success_count=success_count,
            failed_count=failed_count,
            results=ranked,
        )

    def _evaluate_one(self, *, policy: BudgetPolicyCandidate, request: BudgetBatchPolicyEvaluationRequest, index: int) -> dict[str, Any]:
        try:
            result = BudgetPolicyEvaluationService(config=self.config).run(
                BudgetPolicyEvaluationRequest(
                    budget_run_state_path=request.budget_run_state_path,
                    policy_dir=policy.policy_dir,
                    policy_config_path=policy.policy_config_path,
                    param_space_path=policy.param_space_path,
                    policy_spec_path=policy.policy_spec_path,
                    policy_meta_path=policy.policy_meta_path,
                    policy_name=policy.policy_name,
                    search_id=policy.search_id,
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
                    random_seed=request.random_seed + index - 1,
                    benchmark=request.benchmark,
                    stage_chart=request.stage_chart,
                    update_run_state=False,
                    generate_chart=request.generate_chart,
                )
            )
            return result.summary_row
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "search_id": policy.search_id,
                "policy_name": policy.policy_name,
                "policy_dir": str(self._relative(policy.policy_dir)) if policy.policy_dir else None,
                "policy_config_path": str(self._relative(policy.policy_config_path)) if policy.policy_config_path else None,
                "param_space_path": str(self._relative(policy.param_space_path)) if policy.param_space_path else None,
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            }

    def _collect_policies(self, request: BudgetBatchPolicyEvaluationRequest) -> list[BudgetPolicyCandidate]:
        policies: list[BudgetPolicyCandidate] = list(request.policies)
        if request.policy_manifest_path:
            manifest_path = self._resolve_path(request.policy_manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_items = manifest.get("policies", manifest) if isinstance(manifest, dict) else manifest
            if not isinstance(raw_items, list):
                raise ValueError("policy_manifest_path 必须是 JSON 数组，或包含 policies 数组的 JSON 对象。")
            policies.extend(BudgetPolicyCandidate(**item) for item in raw_items)
        if request.policies_dir:
            policies_dir = self._resolve_path(request.policies_dir)
            if not policies_dir.exists():
                raise FileNotFoundError(f"policies_dir 不存在：{policies_dir}")
            for child in sorted(path for path in policies_dir.iterdir() if path.is_dir()):
                if (child / "budget_policy_config.json").exists():
                    policies.append(BudgetPolicyCandidate(policy_dir=child, policy_name=child.name))
        return [self._normalize_policy(item) for item in policies]

    def _normalize_policy(self, item: BudgetPolicyCandidate) -> BudgetPolicyCandidate:
        if item.policy_dir:
            policy_dir = self._resolve_path(item.policy_dir)
            item.policy_config_path = item.policy_config_path or policy_dir / "budget_policy_config.json"
            item.param_space_path = item.param_space_path or policy_dir / "param_space.json"
            item.policy_spec_path = item.policy_spec_path or policy_dir / "budget_policy_spec.md"
            item.policy_meta_path = item.policy_meta_path or policy_dir / "budget_policy_meta.json"
            item.policy_name = item.policy_name or policy_dir.name
        required = [item.policy_config_path, item.param_space_path]
        missing = [str(path) for path in required if path is None or not self._resolve_path(path).exists()]
        if missing:
            raise FileNotFoundError(f"预算策略文件不完整：{missing}")
        item.policy_config_path = self._resolve_path(item.policy_config_path)
        item.param_space_path = self._resolve_path(item.param_space_path)
        item.policy_spec_path = self._resolve_path(item.policy_spec_path) if item.policy_spec_path and self._resolve_path(item.policy_spec_path).exists() else None
        item.policy_meta_path = self._resolve_path(item.policy_meta_path) if item.policy_meta_path and self._resolve_path(item.policy_meta_path).exists() else None
        if not item.policy_name:
            try:
                data = json.loads(item.policy_config_path.read_text(encoding="utf-8"))
                item.policy_name = str(data.get("policy_name") or data.get("policy_id") or item.policy_config_path.parent.name)
            except Exception:
                item.policy_name = item.policy_config_path.parent.name
        return item

    def _assign_search_ids(self, *, policies: list[BudgetPolicyCandidate], batch_id: str, search_prefix: str) -> list[BudgetPolicyCandidate]:
        assigned: list[BudgetPolicyCandidate] = []
        seen: set[str] = set()
        prefix = self._safe_name(search_prefix)
        for index, item in enumerate(policies, start=1):
            if item.search_id:
                candidate = self._safe_name(item.search_id)
            else:
                candidate = f"{batch_id}_{prefix}_{index:03d}_{self._safe_name(item.policy_name or str(index))}"
            actual = candidate
            suffix = 2
            while actual in seen:
                actual = f"{candidate}_{suffix}"
                suffix += 1
            seen.add(actual)
            item.search_id = actual
            assigned.append(item)
        return assigned

    def _rank_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def key(item: dict[str, Any]) -> tuple[int, float]:
            if item.get("status") != "success":
                return (0, float("-inf"))
            score = item.get("best_score")
            try:
                value = float(score)
            except (TypeError, ValueError):
                value = float("-inf")
            return (1, value)

        ranked = sorted(results, key=key, reverse=True)
        rank = 1
        for item in ranked:
            item["rank"] = rank if item.get("status") == "success" else None
            if item.get("status") == "success":
                rank += 1
        return ranked

    def _format_markdown(self, summary: dict[str, Any]) -> str:
        lines = [
            "# Budget Batch Policy Evaluation",
            "",
            f"- budget_run_id: {summary.get('budget_run_id')}",
            f"- batch_id: {summary.get('batch_id')}",
            f"- status: {summary.get('status')}",
            f"- attempted_count: {summary.get('attempted_count')}",
            f"- success_count: {summary.get('success_count')}",
            f"- failed_count: {summary.get('failed_count')}",
            f"- best_search_id: {summary.get('best_search_id')}",
            f"- best_policy_name: {summary.get('best_policy_name')}",
            "",
            "| rank | search_id | status | policy | score | full_sharpe | validation_sharpe | wf_mean | wf_std | max_dd | excess | summary | stage |",
            "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
        for item in summary.get("results", []):
            lines.append(
                "| {rank} | {search_id} | {status} | {policy} | {score} | {full_sharpe} | {validation_sharpe} | {wf_mean} | {wf_std} | {max_dd} | {excess} | {summary_path} | {stage_path} |".format(
                    rank=item.get("rank") or "",
                    search_id=item.get("search_id") or "",
                    status=item.get("status") or "",
                    policy=item.get("policy_name") or "",
                    score=self._fmt(item.get("best_score")),
                    full_sharpe=self._fmt(item.get("full_sharpe")),
                    validation_sharpe=self._fmt(item.get("validation_sharpe")),
                    wf_mean=self._fmt(item.get("walk_forward_mean_score")),
                    wf_std=self._fmt(item.get("walk_forward_std_score")),
                    max_dd=self._fmt(item.get("full_max_drawdown")),
                    excess=self._fmt(item.get("full_excess_total_return")),
                    summary_path=item.get("attempt_summary_path") or "",
                    stage_path=item.get("stage_attribution_path") or "",
                )
            )
        lines.append("")
        return "\n".join(lines)

    def _update_run_state(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        batch_id: str,
        summary: dict[str, Any],
        summary_json_path: Path,
        summary_md_path: Path,
        summary_csv_path: Path,
    ) -> None:
        now = datetime.now().isoformat()
        searches = state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("searches", {})
        for item in summary.get("results", []):
            if item.get("status") != "success" or not item.get("search_id"):
                continue
            searches[item["search_id"]] = {
                "search_result_path": item.get("search_result_path"),
                "best_policy_config_path": item.get("best_policy_config_path"),
                "population_summary_path": item.get("population_summary_path"),
                "best_score": item.get("best_score"),
                "best_params": item.get("best_params"),
                "attempt_summary_path": item.get("attempt_summary_path"),
                "attempt_summary_md_path": item.get("attempt_summary_md_path"),
                "stage_attribution_path": item.get("stage_attribution_path"),
                "stage_attribution_csv_path": item.get("stage_attribution_csv_path"),
                "stage_attribution_md_path": item.get("stage_attribution_md_path"),
                "stage_attribution_chart_path": item.get("stage_attribution_chart_path"),
                "summary": {
                    "full_total_return": item.get("full_total_return"),
                    "full_sharpe": item.get("full_sharpe"),
                    "validation_total_return": item.get("validation_total_return"),
                    "validation_sharpe": item.get("validation_sharpe"),
                    "walk_forward_mean_score": item.get("walk_forward_mean_score"),
                },
            }
        state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("batch_evaluations", {})[batch_id] = {
            "summary_json_path": str(self._relative(summary_json_path)),
            "summary_md_path": str(self._relative(summary_md_path)),
            "summary_csv_path": str(self._relative(summary_csv_path)),
            "status": summary.get("status"),
            "attempted_count": summary.get("attempted_count"),
            "success_count": summary.get("success_count"),
            "failed_count": summary.get("failed_count"),
            "best_search_id": summary.get("best_search_id"),
            "best_policy_name": summary.get("best_policy_name"),
        }
        state.setdefault("strategy_search", {})["current_iteration"] = int(state.get("strategy_search", {}).get("current_iteration") or 0) + int(summary.get("success_count") or 0)
        state.setdefault("strategy_search", {}).setdefault("attempt_ids", []).extend(
            [item["search_id"] for item in summary.get("results", []) if item.get("status") == "success" and item.get("search_id")]
        )
        if summary.get("best_search_id"):
            current_best = state.setdefault("strategy_search", {}).get("best_score")
            best_item = next((item for item in summary.get("results", []) if item.get("search_id") == summary.get("best_search_id")), None)
            if best_item and (current_best is None or self._number(best_item.get("best_score")) > self._number(current_best)):
                state["strategy_search"]["best_score"] = self._number(best_item.get("best_score"))
                state["strategy_search"]["best_attempt_id"] = best_item.get("search_id")
        state["strategy_search"]["status"] = "running"
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetBatchPolicyEvaluationService",
                "event": "budget_batch_policy_evaluation_completed",
                "summary": f"预算层批量策略评估完成：成功 {summary.get('success_count')} 个，失败 {summary.get('failed_count')} 个。",
                "batch_id": batch_id,
                "summary_json_path": str(self._relative(summary_json_path)),
                "summary_md_path": str(self._relative(summary_md_path)),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any], batch_id: str) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        policies_dir = state.get("directories", {}).get("policies")
        if policies_dir:
            return self._resolve_path(policies_dir) / "batch_evaluations" / batch_id
        return self.config.root_dir / "artifacts" / "budget_runs" / str(state.get("budget_run_id")) / "policies" / "batch_evaluations" / batch_id

    def _make_batch_id(self) -> str:
        return f"budget_policy_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def _safe_name(self, value: str) -> str:
        text = str(value).strip().replace(".", "_")
        text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_").lower()
        return text or "budget_policy"

    def _number(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("-inf")

    def _fmt(self, value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "" if value is None else str(value)
        if pd.isna(number):
            return ""
        return f"{number:.6g}"

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
