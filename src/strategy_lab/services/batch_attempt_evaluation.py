from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.attempt_evaluation import AttemptEvaluationError, AttemptEvaluationRequest, AttemptEvaluationService
from strategy_lab.services.signal_run import SignalRunManager


class BatchAttemptStrategy(BaseModel):
    strategy_dir: Path | None = None
    attempt_id: str | None = None
    strategy_path: Path | None = None
    strategy_spec_path: Path | None = None
    param_space_path: Path | None = None
    strategy_meta_path: Path | None = None
    strategy_name: str | None = None
    strategy_class_name: str = "Strategy"


class BatchAttemptEvaluationRequest(BaseModel):
    run_state_path: Path
    strategies_dir: Path | None = None
    strategy_manifest_path: Path | None = None
    strategies: list[BatchAttemptStrategy] = Field(default_factory=list)
    attempt_prefix: str = "attempt_alpha"
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
    quantstats_html: bool = False
    stage_chart: bool = True
    output_dir: Path | None = None


class BatchAttemptEvaluationResult(BaseModel):
    run_state_path: Path
    output_dir: Path
    summary_json_path: Path
    summary_md_path: Path
    status: str
    attempted_count: int
    success_count: int
    failed_count: int
    results: list[dict[str, Any]] = Field(default_factory=list)


class BatchAttemptEvaluationService:
    """批量评估多个 SignalStrategy attempt。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)

    def run(self, request: BatchAttemptEvaluationRequest) -> BatchAttemptEvaluationResult:
        strategies = self._collect_strategies(request)
        if not strategies:
            raise ValueError("没有找到可评估的策略目录或策略配置。")

        assigned = self._assign_attempt_ids(run_state_path=request.run_state_path, strategies=strategies, attempt_prefix=request.attempt_prefix)
        output_dir = self._resolve_output_dir(request)
        output_dir.mkdir(parents=True, exist_ok=True)

        if request.batch_workers <= 1 or len(assigned) <= 1:
            results = [self._evaluate_one(strategy=strategy, request=request, index=index) for index, strategy in enumerate(assigned, start=1)]
        else:
            results_by_attempt: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=max(1, request.batch_workers)) as executor:
                futures = {
                    executor.submit(self._evaluate_one, strategy=strategy, request=request, index=index): strategy.attempt_id or str(index)
                    for index, strategy in enumerate(assigned, start=1)
                }
                for future in as_completed(futures):
                    key = futures[future]
                    results_by_attempt[key] = future.result()
            results = [results_by_attempt[str(strategy.attempt_id)] for strategy in assigned if str(strategy.attempt_id) in results_by_attempt]

        success_count = sum(1 for item in results if item.get("status") == "success")
        failed_count = len(results) - success_count
        status = "success" if success_count == len(results) else "failed" if success_count == 0 else "partial_success"
        summary = {
            "created_at": datetime.now().isoformat(),
            "run_state_path": str(self._resolve_path(request.run_state_path)),
            "status": status,
            "attempted_count": len(results),
            "success_count": success_count,
            "failed_count": failed_count,
            "results": results,
        }
        summary_json_path = output_dir / "batch_evaluation_summary.json"
        summary_md_path = output_dir / "batch_evaluation_summary.md"
        summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary_md_path.write_text(self._format_markdown(summary), encoding="utf-8")

        self.run_manager.append_event(
            request.run_state_path,
            actor="BatchAttemptEvaluationService",
            event="batch_attempt_evaluation_completed",
            summary=f"批量评估完成：成功 {success_count} 个，失败 {failed_count} 个。",
            extra={
                "status": status,
                "summary_json_path": self._relative(summary_json_path),
                "summary_md_path": self._relative(summary_md_path),
            },
        )
        state = self.run_manager.load_state(request.run_state_path)
        reports = state.setdefault("artifacts", {}).setdefault("run_reports", {})
        reports[f"batch_evaluation_{datetime.now().strftime('%Y%m%d_%H%M%S')}"] = {
            "json_path": self._relative(summary_json_path),
            "md_path": self._relative(summary_md_path),
            "status": status,
        }
        self.run_manager.save_state(request.run_state_path, state)

        return BatchAttemptEvaluationResult(
            run_state_path=self._resolve_path(request.run_state_path),
            output_dir=output_dir,
            summary_json_path=summary_json_path,
            summary_md_path=summary_md_path,
            status=status,
            attempted_count=len(results),
            success_count=success_count,
            failed_count=failed_count,
            results=results,
        )

    def _evaluate_one(self, *, strategy: BatchAttemptStrategy, request: BatchAttemptEvaluationRequest, index: int) -> dict[str, Any]:
        try:
            result = AttemptEvaluationService(config=self.config).run(
                AttemptEvaluationRequest(
                    run_state_path=request.run_state_path,
                    attempt_id=strategy.attempt_id,
                    strategy_path=strategy.strategy_path,
                    strategy_spec_path=strategy.strategy_spec_path,
                    param_space_path=strategy.param_space_path,
                    strategy_meta_path=strategy.strategy_meta_path,
                    strategy_name=strategy.strategy_name,
                    strategy_class_name=strategy.strategy_class_name,
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
                    quantstats_html=request.quantstats_html,
                    stage_chart=request.stage_chart,
                )
            )
            return {
                "status": "success",
                "attempt_id": result.attempt_id,
                "strategy_name": result.summary.get("strategy_name"),
                "best_score": result.summary.get("best_score"),
                "full_backtest_dir": result.summary.get("full_backtest_dir"),
                "attempt_summary_path": result.summary.get("attempt_summary_path"),
                "stage_attribution_path": result.summary.get("stage_attribution_path"),
            }
        except AttemptEvaluationError as exc:
            return {
                "status": "failed",
                "attempt_id": strategy.attempt_id,
                "strategy_name": strategy.strategy_name,
                "error_payload": exc.payload,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "attempt_id": strategy.attempt_id,
                "strategy_name": strategy.strategy_name,
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            }

    def _collect_strategies(self, request: BatchAttemptEvaluationRequest) -> list[BatchAttemptStrategy]:
        strategies: list[BatchAttemptStrategy] = list(request.strategies)
        if request.strategy_manifest_path:
            manifest_path = self._resolve_path(request.strategy_manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_items = manifest.get("strategies", manifest) if isinstance(manifest, dict) else manifest
            if not isinstance(raw_items, list):
                raise ValueError("strategy_manifest_path 必须是 JSON 数组，或包含 strategies 数组的 JSON 对象。")
            strategies.extend(BatchAttemptStrategy(**item) for item in raw_items)
        if request.strategies_dir:
            strategies_dir = self._resolve_path(request.strategies_dir)
            if not strategies_dir.exists():
                raise FileNotFoundError(f"strategies_dir 不存在：{strategies_dir}")
            for child in sorted(path for path in strategies_dir.iterdir() if path.is_dir()):
                if (child / "strategy.py").exists():
                    strategies.append(BatchAttemptStrategy(strategy_dir=child, strategy_name=child.name))
        return [self._normalize_strategy(item) for item in strategies]

    def _normalize_strategy(self, item: BatchAttemptStrategy) -> BatchAttemptStrategy:
        if item.strategy_dir:
            strategy_dir = self._resolve_path(item.strategy_dir)
            item.strategy_path = item.strategy_path or strategy_dir / "strategy.py"
            item.strategy_spec_path = item.strategy_spec_path or strategy_dir / "strategy_spec.md"
            item.param_space_path = item.param_space_path or strategy_dir / "param_space.json"
            item.strategy_meta_path = item.strategy_meta_path or strategy_dir / "strategy_meta.json"
            item.strategy_name = item.strategy_name or strategy_dir.name
        required = [item.strategy_path, item.strategy_spec_path, item.param_space_path, item.strategy_meta_path]
        missing = [str(path) for path in required if path is None or not self._resolve_path(path).exists()]
        if missing:
            raise FileNotFoundError(f"策略文件不完整：{missing}")
        item.strategy_path = self._resolve_path(item.strategy_path)
        item.strategy_spec_path = self._resolve_path(item.strategy_spec_path)
        item.param_space_path = self._resolve_path(item.param_space_path)
        item.strategy_meta_path = self._resolve_path(item.strategy_meta_path)
        return item

    def _assign_attempt_ids(
        self,
        *,
        run_state_path: Path,
        strategies: list[BatchAttemptStrategy],
        attempt_prefix: str,
    ) -> list[BatchAttemptStrategy]:
        state = self.run_manager.load_state(run_state_path)
        existing = {str(item.get("attempt_id")) for item in state.get("attempts", []) if item.get("attempt_id")}
        assigned: list[BatchAttemptStrategy] = []
        for index, item in enumerate(strategies, start=1):
            if item.attempt_id:
                candidate = item.attempt_id
            else:
                name_part = self._safe_name(item.strategy_name or f"{index:03d}")
                candidate = f"{attempt_prefix}_{index:03d}_{name_part}"
            actual = candidate
            suffix = 2
            while actual in existing:
                actual = f"{candidate}_{suffix}"
                suffix += 1
            existing.add(actual)
            item.attempt_id = actual
            assigned.append(item)
        return assigned

    def _resolve_output_dir(self, request: BatchAttemptEvaluationRequest) -> Path:
        if request.output_dir:
            return self._resolve_path(request.output_dir)
        state = self.run_manager.load_state(request.run_state_path)
        reports_dir = self._resolve_path(state.get("directories", {}).get("reports", "artifacts/signal_runs/reports"))
        return reports_dir / f"batch_evaluation_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def _format_markdown(self, summary: dict[str, Any]) -> str:
        lines = [
            "# Batch Attempt Evaluation",
            "",
            f"- Status: {summary.get('status')}",
            f"- Attempted: {summary.get('attempted_count')}",
            f"- Success: {summary.get('success_count')}",
            f"- Failed: {summary.get('failed_count')}",
            "",
            "| attempt_id | status | strategy | best_score | summary | stage_attribution |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
        for item in summary.get("results", []):
            lines.append(
                "| {attempt_id} | {status} | {strategy} | {score} | {summary_path} | {stage_path} |".format(
                    attempt_id=item.get("attempt_id"),
                    status=item.get("status"),
                    strategy=item.get("strategy_name") or "",
                    score=item.get("best_score") if item.get("best_score") is not None else "",
                    summary_path=item.get("attempt_summary_path") or "",
                    stage_path=item.get("stage_attribution_path") or "",
                )
            )
        lines.append("")
        return "\n".join(lines)

    def _safe_name(self, value: str) -> str:
        text = value.strip().replace(".", "_")
        text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text)
        text = re.sub(r"_+", "_", text).strip("_").lower()
        return text or "strategy"

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(path)
