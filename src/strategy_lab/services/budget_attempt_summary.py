from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_run import BudgetRunManager


class BudgetAttemptSummaryRequest(BaseModel):
    budget_run_state_path: Path
    search_id: str | None = None
    search_result_path: Path | None = None
    output_json_path: Path | None = None
    output_md_path: Path | None = None
    update_run_state: bool = True

    @model_validator(mode="after")
    def _validate_locator(self) -> "BudgetAttemptSummaryRequest":
        if not self.search_id and not self.search_result_path:
            raise ValueError("必须提供 search_id 或 search_result_path。")
        return self


class BudgetAttemptSummaryResult(BaseModel):
    budget_run_state_path: Path
    search_id: str
    summary_json_path: Path
    summary_md_path: Path
    summary: dict[str, Any] = Field(default_factory=dict)


class BudgetAttemptSummaryService:
    """预算层搜索结果摘要服务。

    该服务不重新执行策略、参数搜索或回测，只读取 search-policy 已经生成的
    搜索结果、最佳参数、分段回测、walk-forward 和画像材料，整理成后续
    BudgetCritic 可以稳定读取的结构化输入。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetAttemptSummaryRequest) -> BudgetAttemptSummaryResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        search_result_path = self._resolve_search_result_path(request, state=state)
        search_result = self._read_json(search_result_path)
        search_id = str(search_result.get("search_id") or request.search_id or search_result_path.parent.name)
        search_dir = search_result_path.parent

        summary = self._build_summary(
            state=state,
            search_id=search_id,
            search_result=search_result,
            search_result_path=search_result_path,
            search_dir=search_dir,
        )
        summary_json_path = self._resolve_output_path(request.output_json_path, default=search_dir / "attempt_summary.json")
        summary_md_path = self._resolve_output_path(request.output_md_path, default=search_dir / "attempt_summary.md")
        summary_json_path.parent.mkdir(parents=True, exist_ok=True)
        summary_md_path.parent.mkdir(parents=True, exist_ok=True)
        summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary_md_path.write_text(self._format_markdown(summary), encoding="utf-8")

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                search_id=search_id,
                summary_json_path=summary_json_path,
                summary_md_path=summary_md_path,
                summary=summary,
            )

        return BudgetAttemptSummaryResult(
            budget_run_state_path=state_path,
            search_id=search_id,
            summary_json_path=summary_json_path,
            summary_md_path=summary_md_path,
            summary=summary,
        )

    def _build_summary(
        self,
        *,
        state: dict[str, Any],
        search_id: str,
        search_result: dict[str, Any],
        search_result_path: Path,
        search_dir: Path,
    ) -> dict[str, Any]:
        best_individual_path = self._path_from_result(search_result, "best_individual_path", search_dir / "best_individual.json")
        walk_forward_summary_path = self._path_from_result(search_result, "walk_forward_summary_path", search_dir / "walk_forward_summary.json")
        population_summary_path = self._path_from_result(search_result, "population_summary_path", search_dir / "population_summary.csv")
        population_json_path = self._path_from_result(search_result, "population_json_path", search_dir / "population_summary.json")
        best_policy_config_path = self._path_from_result(search_result, "best_policy_config_path", search_dir / "best" / "budget_policy_config.json")
        param_space_path = search_dir / "best" / "param_space.json"

        full_dir = self._path_from_result(search_result, "full_output_dir", search_dir / "best" / "full")
        train_dir = self._path_from_result(search_result, "train_output_dir", search_dir / "best" / "train")
        validation_dir = self._path_from_result(search_result, "validation_output_dir", search_dir / "best" / "validation")

        full = self._segment_summary("full", full_dir)
        train = self._segment_summary("train", train_dir)
        validation = self._segment_summary("validation", validation_dir)
        walk_forward = self._read_json_if_exists(walk_forward_summary_path)
        best_individual = self._read_json_if_exists(best_individual_path)
        best_policy_config = self._read_json_if_exists(best_policy_config_path)

        profile_artifacts = state.get("artifacts", {}).get("profile", {}).get("budget_profile", {})
        profile_state = state.get("budget_profile", {})
        data_panel_state = state.get("data_panel", {})
        data_split_state = state.get("data_split", {})

        return {
            "schema_version": "0.1.0",
            "created_at": datetime.now().isoformat(),
            "budget_run_id": state.get("budget_run_id"),
            "search_id": search_id,
            "task": state.get("task"),
            "data": {
                "data_panel": data_panel_state,
                "data_split": data_split_state,
                "profile": profile_state,
                "profile_artifacts": profile_artifacts,
            },
            "policy": {
                "best_policy_config_path": str(self._relative(best_policy_config_path)) if best_policy_config_path.exists() else None,
                "param_space_path": str(self._relative(param_space_path)) if param_space_path.exists() else None,
                "policy_id": best_policy_config.get("policy_id") or best_policy_config.get("policy_name"),
                "policy_name": best_policy_config.get("policy_name"),
                "policy_config": best_policy_config,
            },
            "optimization": {
                "search_result_path": str(self._relative(search_result_path)),
                "search_result": search_result,
                "best_individual_path": str(self._relative(best_individual_path)) if best_individual_path.exists() else None,
                "best_individual": best_individual,
                "best_params": search_result.get("best_params") or best_individual.get("params"),
                "best_score": search_result.get("best_score") or best_individual.get("score"),
                "population_summary_path": str(self._relative(population_summary_path)) if population_summary_path.exists() else None,
                "population_json_path": str(self._relative(population_json_path)) if population_json_path.exists() else None,
                "population_overview": self._population_overview(population_summary_path),
                "walk_forward_summary_path": str(self._relative(walk_forward_summary_path)) if walk_forward_summary_path.exists() else None,
                "walk_forward": walk_forward,
            },
            "backtests": {
                "full": full,
                "train": train,
                "validation": validation,
                "walk_forward": walk_forward,
            },
            "critic_inputs": self._critic_inputs(
                state=state,
                search_result_path=search_result_path,
                search_dir=search_dir,
                best_policy_config_path=best_policy_config_path,
                param_space_path=param_space_path,
                population_summary_path=population_summary_path,
                best_individual_path=best_individual_path,
                walk_forward_summary_path=walk_forward_summary_path,
                full=full,
                train=train,
                validation=validation,
            ),
        }

    def _segment_summary(self, segment: str, segment_dir: Path) -> dict[str, Any]:
        execution_manifest_path = segment_dir / "execution" / "policy_execution_manifest.json"
        backtest_manifest_path = segment_dir / "backtest" / "budget_backtest_manifest.json"
        execution_manifest = self._read_json_if_exists(execution_manifest_path)
        backtest_manifest = self._read_json_if_exists(backtest_manifest_path)
        metrics = dict(backtest_manifest.get("metrics") or {})
        if not metrics:
            metrics_path = segment_dir / "backtest" / "metrics.json"
            metrics = self._read_json_if_exists(metrics_path)
        return {
            "segment": segment,
            "output_dir": str(self._relative(segment_dir)) if segment_dir.exists() else None,
            "execution_manifest_path": str(self._relative(execution_manifest_path)) if execution_manifest_path.exists() else None,
            "backtest_manifest_path": str(self._relative(backtest_manifest_path)) if backtest_manifest_path.exists() else None,
            "metrics_path": self._manifest_path(backtest_manifest, "metrics_path", segment_dir / "backtest" / "metrics.json"),
            "report_path": self._manifest_path(backtest_manifest, "report_path", segment_dir / "backtest" / "report.md"),
            "equity_curve_path": self._manifest_path(backtest_manifest, "equity_curve_path", segment_dir / "backtest" / "equity_curve.parquet"),
            "benchmark_curve_path": self._manifest_path(backtest_manifest, "benchmark_curve_path", segment_dir / "backtest" / "benchmark_curve.parquet"),
            "orders_path": self._manifest_path(backtest_manifest, "orders_path", segment_dir / "backtest" / "orders.parquet"),
            "holdings_path": self._manifest_path(backtest_manifest, "holdings_path", segment_dir / "backtest" / "holdings.parquet"),
            "comparison_chart_path": self._manifest_path(backtest_manifest, "comparison_chart_path", segment_dir / "backtest" / "budget_vs_benchmark.png"),
            "execution_manifest": execution_manifest,
            "backtest_manifest": backtest_manifest,
            "metrics": metrics,
        }

    def _critic_inputs(
        self,
        *,
        state: dict[str, Any],
        search_result_path: Path,
        search_dir: Path,
        best_policy_config_path: Path,
        param_space_path: Path,
        population_summary_path: Path,
        best_individual_path: Path,
        walk_forward_summary_path: Path,
        full: dict[str, Any],
        train: dict[str, Any],
        validation: dict[str, Any],
    ) -> list[str]:
        profile_artifacts = state.get("artifacts", {}).get("profile", {}).get("budget_profile", {})
        profile_paths = [
            profile_artifacts.get("json_path"),
            profile_artifacts.get("markdown_path"),
            profile_artifacts.get("asset_metadata"),
            profile_artifacts.get("asset_summary"),
            profile_artifacts.get("correlation_matrix"),
        ]
        charts = profile_artifacts.get("charts", {})
        if isinstance(charts, dict):
            profile_paths.extend(charts.values())
        paths: list[str | Path | None] = [
            *profile_paths,
            search_result_path,
            search_dir / "search_request.json",
            best_policy_config_path,
            param_space_path,
            population_summary_path,
            search_dir / "population_summary.json",
            best_individual_path,
            walk_forward_summary_path,
            full.get("metrics_path"),
            full.get("report_path"),
            full.get("comparison_chart_path"),
            full.get("equity_curve_path"),
            full.get("orders_path"),
            full.get("holdings_path"),
            train.get("metrics_path"),
            validation.get("metrics_path"),
        ]
        result: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if not path:
                continue
            resolved = self._resolve_path(path)
            if not resolved.exists():
                continue
            item = str(self._relative(resolved))
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    def _population_overview(self, population_summary_path: Path) -> dict[str, Any]:
        if not population_summary_path.exists():
            return {}
        df = pd.read_csv(population_summary_path)
        if df.empty:
            return {"candidate_count": 0, "top_candidates": []}
        if "score" in df.columns:
            df["score"] = pd.to_numeric(df["score"], errors="coerce")
            df = df.sort_values("score", ascending=False)
        overview = {
            "candidate_count": int(len(df)),
            "columns": list(df.columns),
            "top_candidates": df.head(10).to_dict(orient="records"),
        }
        if "score" in df.columns:
            overview.update(
                {
                    "score_max": self._safe_float(df["score"].max()),
                    "score_min": self._safe_float(df["score"].min()),
                    "score_mean": self._safe_float(df["score"].mean()),
                }
            )
        for key in ["train_score", "validation_score", "walk_forward_mean_score", "walk_forward_std_score"]:
            if key in df.columns:
                series = pd.to_numeric(df[key], errors="coerce")
                overview[f"{key}_mean"] = self._safe_float(series.mean())
        return overview

    def _format_markdown(self, summary: dict[str, Any]) -> str:
        optimization = summary.get("optimization", {})
        policy = summary.get("policy", {})
        backtests = summary.get("backtests", {})
        score_components = (optimization.get("best_individual") or {}).get("score_components") or {}
        population = optimization.get("population_overview") or {}
        walk_forward = optimization.get("walk_forward") or {}
        lines = [
            "# Budget Attempt Summary",
            "",
            f"- budget_run_id: {summary.get('budget_run_id')}",
            f"- search_id: {summary.get('search_id')}",
            f"- policy_name: {policy.get('policy_name')}",
            f"- best_score: {optimization.get('best_score')}",
            f"- candidate_count: {population.get('candidate_count')}",
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
            "## Backtest Metrics",
            "",
            "| segment | total_return | annual_return | sharpe | max_drawdown | benchmark_total_return | excess_total_return | avg_turnover | avg_gross_exposure | avg_holding_count |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for segment in ["full", "train", "validation"]:
            metrics = (backtests.get(segment) or {}).get("metrics") or {}
            lines.append(
                "| {segment} | {total_return} | {annual_return} | {sharpe} | {max_drawdown} | {benchmark_total_return} | {excess_total_return} | {average_turnover} | {average_gross_exposure} | {average_holding_count} |".format(
                    segment=segment,
                    total_return=self._fmt(metrics.get("total_return")),
                    annual_return=self._fmt(metrics.get("annual_return")),
                    sharpe=self._fmt(metrics.get("sharpe")),
                    max_drawdown=self._fmt(metrics.get("max_drawdown")),
                    benchmark_total_return=self._fmt(metrics.get("benchmark_total_return")),
                    excess_total_return=self._fmt(metrics.get("excess_total_return")),
                    average_turnover=self._fmt(metrics.get("average_turnover")),
                    average_gross_exposure=self._fmt(metrics.get("average_gross_exposure")),
                    average_holding_count=self._fmt(metrics.get("average_holding_count")),
                )
            )
        lines.extend(
            [
                "",
                "## Walk Forward",
                "",
                f"- fold_count: {walk_forward.get('fold_count')}",
                f"- mean_score: {walk_forward.get('mean_score')}",
                f"- std_score: {walk_forward.get('std_score')}",
                f"- min_score: {walk_forward.get('min_score')}",
                f"- max_score: {walk_forward.get('max_score')}",
                "",
                "| fold | score | total_return | sharpe | max_drawdown | benchmark_total_return | avg_turnover |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for fold in walk_forward.get("folds", [])[:20]:
            lines.append(
                "| {fold_id} | {score} | {total_return} | {sharpe} | {max_drawdown} | {benchmark_total_return} | {average_turnover} |".format(
                    fold_id=fold.get("fold_id"),
                    score=self._fmt(fold.get("score")),
                    total_return=self._fmt(fold.get("metrics_total_return")),
                    sharpe=self._fmt(fold.get("metrics_sharpe")),
                    max_drawdown=self._fmt(fold.get("metrics_max_drawdown")),
                    benchmark_total_return=self._fmt(fold.get("metrics_benchmark_total_return")),
                    average_turnover=self._fmt(fold.get("metrics_average_turnover")),
                )
            )
        lines.extend(
            [
                "",
                "## Top Candidates",
                "",
                "| candidate | score | train | validation | walk_forward_mean | walk_forward_std | cache_hit |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for candidate in population.get("top_candidates", [])[:10]:
            lines.append(
                "| {candidate_id} | {score} | {train_score} | {validation_score} | {wf_mean} | {wf_std} | {cache_hit} |".format(
                    candidate_id=candidate.get("candidate_id"),
                    score=self._fmt(candidate.get("score")),
                    train_score=self._fmt(candidate.get("train_score")),
                    validation_score=self._fmt(candidate.get("validation_score")),
                    wf_mean=self._fmt(candidate.get("walk_forward_mean_score")),
                    wf_std=self._fmt(candidate.get("walk_forward_std_score")),
                    cache_hit=candidate.get("cache_hit"),
                )
            )
        lines.extend(["", "## Key Files", ""])
        for path in summary.get("critic_inputs", []):
            lines.append(f"- {path}")
        lines.append("")
        return "\n".join(lines)

    def _resolve_search_result_path(self, request: BudgetAttemptSummaryRequest, *, state: dict[str, Any]) -> Path:
        if request.search_result_path:
            return self._resolve_path(request.search_result_path)
        searches = state.get("artifacts", {}).get("policies", {}).get("searches", {})
        if request.search_id in searches:
            search_result_path = searches[request.search_id].get("search_result_path")
            if search_result_path:
                return self._resolve_path(search_result_path)
        policies_dir = state.get("directories", {}).get("policies")
        if policies_dir:
            return self._resolve_path(policies_dir) / "searches" / str(request.search_id) / "search_result.json"
        return self.config.root_dir / "artifacts" / "budget_runs" / str(state.get("budget_run_id")) / "policies" / "searches" / str(request.search_id) / "search_result.json"

    def _path_from_result(self, search_result: dict[str, Any], key: str, default: Path) -> Path:
        value = search_result.get(key)
        return self._resolve_path(value) if value else default

    def _manifest_path(self, manifest: dict[str, Any], key: str, default: Path) -> str | None:
        value = manifest.get(key)
        path = self._resolve_path(value) if value else default
        return str(self._relative(path)) if path.exists() else None

    def _resolve_output_path(self, value: Path | None, *, default: Path) -> Path:
        return self._resolve_path(value) if value else default

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_json_if_exists(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _update_run_state(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        search_id: str,
        summary_json_path: Path,
        summary_md_path: Path,
        summary: dict[str, Any],
    ) -> None:
        entry = state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("searches", {}).setdefault(search_id, {})
        entry.update(
            {
                "attempt_summary_path": str(self._relative(summary_json_path)),
                "attempt_summary_md_path": str(self._relative(summary_md_path)),
                "best_score": summary.get("optimization", {}).get("best_score"),
                "summary": {
                    "full_total_return": summary.get("backtests", {}).get("full", {}).get("metrics", {}).get("total_return"),
                    "full_sharpe": summary.get("backtests", {}).get("full", {}).get("metrics", {}).get("sharpe"),
                    "validation_total_return": summary.get("backtests", {}).get("validation", {}).get("metrics", {}).get("total_return"),
                    "walk_forward_mean_score": summary.get("optimization", {}).get("walk_forward", {}).get("mean_score"),
                },
            }
        )
        state.setdefault("events", []).append(
            {
                "time": datetime.now().isoformat(),
                "actor": "BudgetAttemptSummaryService",
                "event": "budget_attempt_summary_created",
                "summary": f"预算层搜索 {search_id} 已生成评估摘要。",
                "extra": {
                    "search_id": search_id,
                    "attempt_summary_path": str(self._relative(summary_json_path)),
                    "attempt_summary_md_path": str(self._relative(summary_md_path)),
                },
            }
        )
        state["updated_at"] = datetime.now().isoformat()
        self.run_manager.save_state(state_path, state)

    def _safe_float(self, value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(number):
            return None
        return number

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

    def _relative(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.config.root_dir.resolve())
        except ValueError:
            return path
