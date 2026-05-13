from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.attempt_summary import AttemptSummaryRequest, AttemptSummaryResult, AttemptSummaryService
from strategy_lab.services.parameter_search import ParameterSearchRequest, ParameterSearchResult, ParameterSearchService
from strategy_lab.services.signal_run import SignalRunManager
from strategy_lab.services.stage_attribution import StageAttributionRequest, StageAttributionResult, StageAttributionService
from strategy_lab.services.strategy_artifact import StrategyArtifactRequest, StrategyArtifactResult, StrategyArtifactService


class AttemptEvaluationRequest(BaseModel):
    run_state_path: Path
    attempt_id: str | None = None
    strategy_path: Path | None = None
    strategy_spec_path: Path | None = None
    param_space_path: Path | None = None
    strategy_meta_path: Path | None = None
    template: str | None = None
    strategy_name: str | None = None
    strategy_class_name: str = "Strategy"
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
    quantstats_html: bool = False
    stage_chart: bool = True


class AttemptEvaluationResult(BaseModel):
    run_state_path: Path
    attempt_id: str
    strategy_result: StrategyArtifactResult
    parameter_search_result: ParameterSearchResult
    attempt_summary_result: AttemptSummaryResult
    stage_attribution_result: StageAttributionResult
    summary: dict[str, Any] = Field(default_factory=dict)


class AttemptEvaluationError(RuntimeError):
    """带结构化 payload 的 attempt evaluation 错误。"""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(json.dumps(payload, ensure_ascii=False, default=str))


class AttemptEvaluationService:
    """单个 attempt 的复盘前准备流水线。

    SignalAgent 写好策略文件后调用本服务。本服务把创建 attempt、保存策略、
    参数搜索、回测、摘要和阶段归因固定成一条确定性流程。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)
        self.strategy_service = StrategyArtifactService(config=self.config)
        self.search_service = ParameterSearchService(config=self.config)
        self.summary_service = AttemptSummaryService(config=self.config)
        self.stage_service = StageAttributionService(config=self.config)

    def run(self, request: AttemptEvaluationRequest) -> AttemptEvaluationResult:
        attempt_id = request.attempt_id or self.run_manager.new_attempt_id(request.run_state_path)
        completed_steps: list[str] = []
        result_paths: dict[str, str] = {}
        try:
            if not self._attempt_exists(request.run_state_path, attempt_id):
                self.run_manager.create_attempt(request.run_state_path, attempt_id=attempt_id)
            completed_steps.append("create_attempt")

            strategy_result = self.strategy_service.run(
                StrategyArtifactRequest(
                    run_state_path=request.run_state_path,
                    attempt_id=attempt_id,
                    strategy_path=request.strategy_path,
                    strategy_spec_path=request.strategy_spec_path,
                    param_space_path=request.param_space_path,
                    strategy_meta_path=request.strategy_meta_path,
                    template=request.template,
                    strategy_name=request.strategy_name,
                    strategy_class_name=request.strategy_class_name,
                )
            )
            completed_steps.append("save_strategy")
            result_paths.update(
                {
                    "strategy_path": str(strategy_result.strategy_path),
                    "param_space_path": str(strategy_result.param_space_path),
                    "strategy_meta_path": str(strategy_result.strategy_meta_path),
                }
            )

            search_result = self.search_service.run(
                ParameterSearchRequest(
                    run_state_path=request.run_state_path,
                    attempt_id=attempt_id,
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
                    quantstats_html=request.quantstats_html,
                )
            )
            completed_steps.append("parameter_search")
            result_paths.update(
                {
                    "best_individual_path": str(search_result.best_individual_path),
                    "population_summary_path": str(search_result.population_summary_path),
                    "search_result_path": str(search_result.search_result_path),
                    "full_backtest_dir": str(search_result.full_backtest_dir),
                    "train_backtest_dir": str(search_result.train_backtest_dir),
                    "validation_backtest_dir": str(search_result.validation_backtest_dir),
                    "walk_forward_summary_path": str(search_result.walk_forward_summary_path) if search_result.walk_forward_summary_path else "",
                }
            )

            summary_result = self.summary_service.run(AttemptSummaryRequest(run_state_path=request.run_state_path, attempt_id=attempt_id))
            completed_steps.append("attempt_summary")
            result_paths.update(
                {
                    "attempt_summary_path": str(summary_result.summary_json_path),
                    "attempt_summary_md_path": str(summary_result.summary_md_path),
                }
            )

            stage_result = self.stage_service.run(
                StageAttributionRequest(
                    run_state_path=request.run_state_path,
                    attempt_id=attempt_id,
                    generate_chart=request.stage_chart,
                )
            )
            completed_steps.append("stage_attribution")
            result_paths.update(
                {
                    "stage_attribution_path": str(stage_result.json_path),
                    "stage_attribution_md_path": str(stage_result.markdown_path),
                    "stage_attribution_chart_path": str(stage_result.chart_path) if stage_result.chart_path else "",
                }
            )

            self.run_manager.append_event(
                request.run_state_path,
                actor="AttemptEvaluationService",
                event="attempt_evaluation_completed",
                summary=f"{attempt_id} 已完成策略评估与复盘前数据准备。",
                extra={
                    "attempt_id": attempt_id,
                    "best_score": search_result.best_score,
                    "attempt_summary_path": self._relative(summary_result.summary_json_path),
                    "stage_attribution_path": self._relative(stage_result.json_path),
                },
            )
            return AttemptEvaluationResult(
                run_state_path=self._resolve_path(request.run_state_path),
                attempt_id=attempt_id,
                strategy_result=strategy_result,
                parameter_search_result=search_result,
                attempt_summary_result=summary_result,
                stage_attribution_result=stage_result,
                summary={
                    "attempt_id": attempt_id,
                    "strategy_name": strategy_result.summary.get("strategy_name"),
                    "best_score": search_result.best_score,
                    "best_params": search_result.best_params,
                    "status": "ready_for_review",
                    "full_backtest_dir": str(search_result.full_backtest_dir),
                    "attempt_summary_path": str(summary_result.summary_json_path),
                    "attempt_summary_md_path": str(summary_result.summary_md_path),
                    "stage_attribution_path": str(stage_result.json_path),
                    "stage_attribution_md_path": str(stage_result.markdown_path),
                    "stage_attribution_chart_path": str(stage_result.chart_path) if stage_result.chart_path else None,
                },
            )
        except Exception as exc:
            failed_step = self._infer_failed_step(completed_steps)
            payload = self._handle_failure(
                request=request,
                attempt_id=attempt_id,
                failed_step=failed_step,
                completed_steps=completed_steps,
                result_paths=result_paths,
                error=exc,
            )
            raise AttemptEvaluationError(payload) from exc

    def _handle_failure(
        self,
        *,
        request: AttemptEvaluationRequest,
        attempt_id: str,
        failed_step: str,
        completed_steps: list[str],
        result_paths: dict[str, str],
        error: Exception,
    ) -> dict[str, Any]:
        error_dir = self._attempt_error_dir(request.run_state_path, attempt_id)
        error_dir.mkdir(parents=True, exist_ok=True)
        error_path = error_dir / "attempt_evaluation_error.json"
        payload = {
            "status": "failed",
            "attempt_id": attempt_id,
            "failed_step": failed_step,
            "completed_steps": completed_steps,
            "result_paths": result_paths,
            "error_type": error.__class__.__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "created_at": datetime.now().isoformat(),
            "next_action": self._failure_next_action(failed_step),
        }
        error_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        try:
            self.run_manager.update_attempt(
                request.run_state_path,
                attempt_id=attempt_id,
                status="failed",
                fields={
                    "attempt_evaluation_error_path": self._relative(error_path),
                    "failed_step": failed_step,
                },
            )
            self.run_manager.append_event(
                request.run_state_path,
                actor="AttemptEvaluationService",
                event="attempt_evaluation_failed",
                summary=f"{attempt_id} 在 {failed_step} 阶段失败：{error}",
                extra={
                    "attempt_id": attempt_id,
                    "failed_step": failed_step,
                    "error_path": self._relative(error_path),
                },
            )
        except Exception:
            payload["run_state_update_error"] = traceback.format_exc()
            error_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        payload["error_path"] = str(error_path)
        return payload

    def _infer_failed_step(self, completed_steps: list[str]) -> str:
        order = ["create_attempt", "save_strategy", "parameter_search", "attempt_summary", "stage_attribution"]
        for step in order:
            if step not in completed_steps:
                return step
        return "finalize"

    def _failure_next_action(self, failed_step: str) -> str:
        hints = {
            "create_attempt": "检查 run_state_path 是否存在且 JSON 可解析。",
            "save_strategy": "检查 strategy.py 路径、策略类名、suggest 接口、strategy_spec/param_space/meta 文件格式。",
            "parameter_search": "检查 data-split 是否已生成、param_space.json 是否合理、策略 suggest 是否能在历史数据上稳定运行。",
            "attempt_summary": "检查参数搜索是否生成 full/train/validation/walk-forward 回测产物。",
            "stage_attribution": "检查 market-profile 是否已生成，以及 full 回测目录中 equity_curve、orders、daily_signals、metrics 是否存在。",
            "finalize": "检查 run_state.json 写入权限和事件追加逻辑。",
        }
        return hints.get(failed_step, "查看 error_path 中的 traceback，修复后重新运行 evaluate-attempt。")

    def _attempt_error_dir(self, run_state_path: Path, attempt_id: str) -> Path:
        state = self.run_manager.load_state(run_state_path)
        attempt = next((item for item in state.get("attempts", []) if item.get("attempt_id") == attempt_id), None)
        if attempt and attempt.get("logs_dir"):
            return self._resolve_path(attempt["logs_dir"])
        root = self._resolve_path(state.get("directories", {}).get("attempts", "artifacts/signal_runs/attempts"))
        return root / attempt_id / "logs"

    def _attempt_exists(self, run_state_path: Path, attempt_id: str) -> bool:
        state = self.run_manager.load_state(run_state_path)
        return any(item.get("attempt_id") == attempt_id for item in state.get("attempts", []))

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
