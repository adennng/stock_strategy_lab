from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.signal_run import SignalRunManager


class AttemptSummaryRequest(BaseModel):
    run_state_path: Path
    attempt_id: str


class AttemptSummaryResult(BaseModel):
    run_state_path: Path
    attempt_id: str
    summary_json_path: Path
    summary_md_path: Path
    summary: dict[str, Any] = Field(default_factory=dict)


class AttemptSummaryService:
    """attempt 汇总服务。

    汇总策略、参数搜索、最佳回测和 walk-forward 结果，
    为 CriticAgent 提供稳定、结构化的复盘输入。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)

    def run(self, request: AttemptSummaryRequest) -> AttemptSummaryResult:
        state = self.run_manager.load_state(request.run_state_path)
        attempt = self._find_attempt(state, request.attempt_id)
        optimization_dir = self._resolve_path(attempt["optimization_dir"])
        optimization_dir.mkdir(parents=True, exist_ok=True)

        summary = self._build_summary(state=state, attempt=attempt)
        summary_json_path = optimization_dir / "attempt_summary.json"
        summary_md_path = optimization_dir / "attempt_summary.md"
        summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary_md_path.write_text(self._format_markdown(summary), encoding="utf-8")

        self.run_manager.update_attempt(
            request.run_state_path,
            attempt_id=request.attempt_id,
            status="ready_for_review",
            score=summary.get("score"),
            fields={
                "attempt_summary_path": str(self._relative(summary_json_path)),
                "attempt_summary_md_path": str(self._relative(summary_md_path)),
            },
        )
        self.run_manager.append_event(
            request.run_state_path,
            actor="AttemptSummaryService",
            event="attempt_summary_created",
            summary=f"{request.attempt_id} 已生成复盘输入摘要。",
            extra={"attempt_id": request.attempt_id, "attempt_summary_path": str(self._relative(summary_json_path))},
        )

        return AttemptSummaryResult(
            run_state_path=self._resolve_path(request.run_state_path),
            attempt_id=request.attempt_id,
            summary_json_path=summary_json_path,
            summary_md_path=summary_md_path,
            summary=summary,
        )

    def _build_summary(self, *, state: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
        full_metrics = self._read_json_from_attempt(attempt, "full_metrics_path")
        train_metrics = self._read_json_from_attempt(attempt, "train_metrics_path")
        validation_metrics = self._read_json_from_attempt(attempt, "validation_metrics_path")
        best_individual = self._read_json_from_attempt(attempt, "best_individual_path")
        walk_forward_summary = self._read_json_from_attempt(attempt, "walk_forward_summary_path")
        search_result = self._read_json_from_attempt(attempt, "search_result_path")
        population_summary_path = self._resolve_optional(attempt.get("population_summary_path"))
        population_overview = self._population_overview(population_summary_path)
        market_profile = state.get("artifacts", {}).get("market_profile", {})

        return {
            "created_at": datetime.now().isoformat(),
            "run_id": state.get("run_id"),
            "attempt_id": attempt.get("attempt_id"),
            "status": attempt.get("status"),
            "score": attempt.get("score"),
            "task": state.get("task"),
            "strategy": {
                "strategy_name": attempt.get("strategy_name"),
                "strategy_ref": attempt.get("strategy_ref"),
                "strategy_path": attempt.get("strategy_path"),
                "strategy_spec_path": attempt.get("strategy_spec_path"),
                "param_space_path": attempt.get("param_space_path"),
                "strategy_meta_path": attempt.get("strategy_meta_path"),
                "strategy_meta": self._read_json_from_attempt(attempt, "strategy_meta_path"),
            },
            "market_profile": market_profile,
            "optimization": {
                "best_params": attempt.get("best_params"),
                "best_individual": best_individual,
                "search_result": search_result,
                "population_summary_path": attempt.get("population_summary_path"),
                "population_overview": population_overview,
                "validation_summary_path": attempt.get("validation_summary_path"),
                "walk_forward_summary_path": attempt.get("walk_forward_summary_path"),
            },
            "best_backtests": {
                "full": {
                    "dir": attempt.get("full_backtest_dir"),
                    "metrics_path": attempt.get("full_metrics_path"),
                    "metrics": full_metrics,
                    "comparison_chart_path": self._artifact_path(attempt.get("full_backtest_dir"), "strategy_vs_benchmark.png"),
                },
                "train": {
                    "dir": attempt.get("train_backtest_dir"),
                    "metrics_path": attempt.get("train_metrics_path"),
                    "metrics": train_metrics,
                },
                "validation": {
                    "dir": attempt.get("validation_backtest_dir"),
                    "metrics_path": attempt.get("validation_metrics_path"),
                    "metrics": validation_metrics,
                },
                "walk_forward": walk_forward_summary,
            },
            "critic_inputs": self._critic_inputs(attempt=attempt, market_profile=market_profile),
        }

    def _critic_inputs(self, *, attempt: dict[str, Any], market_profile: dict[str, Any]) -> list[str]:
        primary_profile = market_profile.get("primary", {}) if isinstance(market_profile.get("primary"), dict) else {}
        paths = [
            market_profile.get("profile_path") or primary_profile.get("json_path"),
            market_profile.get("profile_md_path") or primary_profile.get("markdown_path"),
            market_profile.get("chart_path") or primary_profile.get("chart_path"),
            attempt.get("strategy_path"),
            attempt.get("strategy_spec_path"),
            attempt.get("param_space_path"),
            attempt.get("strategy_meta_path"),
            attempt.get("population_summary_path"),
            attempt.get("best_individual_path"),
            attempt.get("validation_summary_path"),
            attempt.get("walk_forward_summary_path"),
            attempt.get("stage_attribution_path"),
            attempt.get("stage_attribution_md_path"),
            attempt.get("stage_attribution_csv_path"),
            attempt.get("stage_attribution_chart_path"),
            attempt.get("full_metrics_path"),
            self._artifact_path(attempt.get("full_backtest_dir"), "report.md"),
            self._artifact_path(attempt.get("full_backtest_dir"), "strategy_vs_benchmark.png"),
            self._artifact_path(attempt.get("full_backtest_dir"), "equity_curve.parquet"),
            self._artifact_path(attempt.get("full_backtest_dir"), "orders.parquet"),
            self._artifact_path(attempt.get("full_backtest_dir"), "daily_signals.parquet"),
        ]
        return [str(path) for path in paths if path]

    def _population_overview(self, population_summary_path: Path | None) -> dict[str, Any]:
        if not population_summary_path or not population_summary_path.exists():
            return {}
        df = pd.read_csv(population_summary_path)
        if df.empty:
            return {"candidate_count": 0}
        score = pd.to_numeric(df["score"], errors="coerce")
        return {
            "candidate_count": int(len(df)),
            "score_max": self._safe_float(score.max()),
            "score_min": self._safe_float(score.min()),
            "score_mean": self._safe_float(score.mean()),
            "top_candidates": df.head(5).to_dict(orient="records"),
        }

    def _format_markdown(self, summary: dict[str, Any]) -> str:
        full_metrics = summary.get("best_backtests", {}).get("full", {}).get("metrics", {}) or {}
        train_metrics = summary.get("best_backtests", {}).get("train", {}).get("metrics", {}) or {}
        validation_metrics = summary.get("best_backtests", {}).get("validation", {}).get("metrics", {}) or {}
        walk_forward = summary.get("best_backtests", {}).get("walk_forward", {}) or {}
        optimization = summary.get("optimization", {})
        best_individual = optimization.get("best_individual") or {}
        score_components = best_individual.get("score_components") or {}
        population_overview = optimization.get("population_overview") or {}

        lines = [
            "# Attempt Summary",
            "",
            f"- Run ID: {summary.get('run_id')}",
            f"- Attempt ID: {summary.get('attempt_id')}",
            f"- Strategy: {summary.get('strategy', {}).get('strategy_name')}",
            f"- Status: {summary.get('status')}",
            f"- Score: {summary.get('score')}",
            "",
            "## Best Params",
            "",
            "```json",
            json.dumps(optimization.get("best_params") or {}, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
            "## Score Components",
            "",
            f"- train_score: {score_components.get('train_score')}",
            f"- validation_score: {score_components.get('validation_score')}",
            f"- walk_forward_mean_score: {score_components.get('walk_forward_mean_score')}",
            f"- walk_forward_std_score: {score_components.get('walk_forward_std_score')}",
            f"- walk_forward_min_score: {score_components.get('walk_forward_min_score')}",
            f"- overfit_penalty: {score_components.get('overfit_penalty')}",
            f"- walk_forward_instability_penalty: {score_components.get('walk_forward_instability_penalty')}",
            "",
            "## Full Backtest",
            "",
        ]
        self._append_metrics(lines, full_metrics)
        lines.extend(["", "## Train Backtest", ""])
        self._append_metrics(lines, train_metrics)
        lines.extend(["", "## Validation Backtest", ""])
        self._append_metrics(lines, validation_metrics)
        lines.extend(["", "## Walk Forward", ""])
        lines.append(f"- fold_count: {walk_forward.get('fold_count')}")
        lines.append(f"- mean_score: {walk_forward.get('mean_score')}")
        lines.append(f"- std_score: {walk_forward.get('std_score')}")
        lines.append(f"- min_score: {walk_forward.get('min_score')}")
        lines.append(f"- max_score: {walk_forward.get('max_score')}")
        lines.append("")
        lines.append("| fold | score | total_return | sharpe | max_drawdown | benchmark_total_return |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for fold in walk_forward.get("folds", [])[:20]:
            lines.append(
                "| {fold_id} | {score} | {total_return} | {sharpe} | {max_drawdown} | {benchmark_total_return} |".format(
                    fold_id=fold.get("fold_id"),
                    score=self._format_number(fold.get("score")),
                    total_return=self._format_number(fold.get("metrics_total_return")),
                    sharpe=self._format_number(fold.get("metrics_sharpe")),
                    max_drawdown=self._format_number(fold.get("metrics_max_drawdown")),
                    benchmark_total_return=self._format_number(fold.get("metrics_benchmark_total_return")),
                )
            )
        lines.extend(["", "## Candidate Overview", ""])
        lines.append(f"- candidate_count: {population_overview.get('candidate_count')}")
        lines.append(f"- score_max: {population_overview.get('score_max')}")
        lines.append(f"- score_min: {population_overview.get('score_min')}")
        lines.append(f"- score_mean: {population_overview.get('score_mean')}")
        lines.append("")
        lines.append("| candidate | score | train | validation | walk_forward_mean | walk_forward_std | cache_hit |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
        for candidate in population_overview.get("top_candidates", [])[:10]:
            lines.append(
                "| {candidate_id} | {score} | {train_score} | {validation_score} | {wf_mean} | {wf_std} | {cache_hit} |".format(
                    candidate_id=candidate.get("candidate_id"),
                    score=self._format_number(candidate.get("score")),
                    train_score=self._format_number(candidate.get("train_score")),
                    validation_score=self._format_number(candidate.get("validation_score")),
                    wf_mean=self._format_number(candidate.get("walk_forward_mean_score")),
                    wf_std=self._format_number(candidate.get("walk_forward_std_score")),
                    cache_hit=candidate.get("cache_hit"),
                )
            )
        lines.extend(["", "## Critic Inputs", ""])
        for path in summary.get("critic_inputs", []):
            lines.append(f"- {path}")
        lines.append("")
        return "\n".join(lines)

    def _append_metrics(self, lines: list[str], metrics: dict[str, Any]) -> None:
        for key in [
            "total_return",
            "annual_return",
            "sharpe",
            "max_drawdown",
            "calmar",
            "excess_total_return",
            "benchmark_total_return",
            "information_ratio",
            "order_count",
            "signal_changes",
        ]:
            lines.append(f"- {key}: {metrics.get(key)}")

    def _read_json_from_attempt(self, attempt: dict[str, Any], field_name: str) -> dict[str, Any]:
        path = self._resolve_optional(attempt.get(field_name))
        if not path or not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_optional(self, value: str | Path | None) -> Path | None:
        if not value:
            return None
        return self._resolve_path(value)

    def _artifact_path(self, directory: str | Path | None, filename: str) -> str | None:
        if not directory:
            return None
        path = self._resolve_path(directory) / filename
        return str(self._relative(path)) if path.exists() else None

    def _find_attempt(self, state: dict[str, Any], attempt_id: str) -> dict[str, Any]:
        attempt = next((item for item in state.get("attempts", []) if item.get("attempt_id") == attempt_id), None)
        if attempt is None:
            raise ValueError(f"run_state.json 中不存在 attempt：{attempt_id}")
        return attempt

    def _safe_float(self, value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(number):
            return None
        return number

    def _format_number(self, value: Any) -> str:
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

    def _relative(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.config.root_dir.resolve())
        except ValueError:
            return path
