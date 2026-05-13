from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.attempt_summary import AttemptSummaryRequest, AttemptSummaryService
from strategy_lab.services.signal_run import SignalRunManager


class AttemptComparisonRequest(BaseModel):
    run_state_path: Path
    attempt_ids: list[str] = Field(default_factory=list)
    output_dir: Path | None = None
    generate_charts: bool = True


class AttemptComparisonResult(BaseModel):
    run_state_path: Path
    attempt_ids: list[str]
    output_dir: Path
    summary_json_path: Path
    summary_csv_path: Path
    report_path: Path
    chart_paths: dict[str, Path] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class AttemptComparisonService:
    """多个 attempt 的横向比较服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)
        self.summary_service = AttemptSummaryService(config=self.config)

    def run(self, request: AttemptComparisonRequest) -> AttemptComparisonResult:
        state = self.run_manager.load_state(request.run_state_path)
        attempts = self._select_attempts(state, request.attempt_ids)
        output_dir = self._resolve_output_dir(request.output_dir, state)
        output_dir.mkdir(parents=True, exist_ok=True)

        rows: list[dict[str, Any]] = []
        for attempt in attempts:
            summary = self._load_or_create_attempt_summary(
                run_state_path=request.run_state_path,
                attempt_id=str(attempt["attempt_id"]),
                attempt=attempt,
            )
            rows.append(self._build_row(summary))

        rows = sorted(rows, key=lambda item: self._sort_score(item), reverse=True)
        for index, row in enumerate(rows, start=1):
            row["rank"] = index

        df = pd.DataFrame(rows)
        summary = {
            "created_at": datetime.now().isoformat(),
            "run_id": state.get("run_id"),
            "attempt_ids": [row.get("attempt_id") for row in rows],
            "attempt_count": len(rows),
            "ranking": rows,
            "best_attempt_id": rows[0].get("attempt_id") if rows else None,
            "selection_basis": [
                "综合分 score",
                "full_sharpe",
                "walk_forward_mean_score",
                "walk_forward_std_score",
                "walk_forward_min_score",
                "max_drawdown",
                "excess_total_return",
                "train_validation_gap",
                "order_count",
            ],
        }

        summary_json_path = output_dir / "comparison_summary.json"
        summary_csv_path = output_dir / "comparison_summary.csv"
        report_path = output_dir / "comparison_report.md"
        summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        df.to_csv(summary_csv_path, index=False, encoding="utf-8-sig")
        report_path.write_text(self._format_markdown(summary), encoding="utf-8")

        chart_paths: dict[str, Path] = {}
        if request.generate_charts and not df.empty:
            chart_paths = self._write_charts(df=df, output_dir=output_dir)

        self.run_manager.append_event(
            request.run_state_path,
            actor="AttemptComparisonService",
            event="attempt_comparison_created",
            summary=f"已生成 {len(rows)} 个 attempt 的横向比较。",
            extra={
                "attempt_ids": [row.get("attempt_id") for row in rows],
                "comparison_summary_path": self._relative(summary_json_path),
            },
        )

        return AttemptComparisonResult(
            run_state_path=self._resolve_path(request.run_state_path),
            attempt_ids=[row.get("attempt_id") for row in rows],
            output_dir=output_dir,
            summary_json_path=summary_json_path,
            summary_csv_path=summary_csv_path,
            report_path=report_path,
            chart_paths=chart_paths,
            summary=summary,
        )

    def _load_or_create_attempt_summary(
        self,
        *,
        run_state_path: Path,
        attempt_id: str,
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        summary_path = self._resolve_optional(attempt.get("attempt_summary_path"))
        if not summary_path or not summary_path.exists():
            result = self.summary_service.run(AttemptSummaryRequest(run_state_path=run_state_path, attempt_id=attempt_id))
            return result.summary
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def _build_row(self, summary: dict[str, Any]) -> dict[str, Any]:
        optimization = summary.get("optimization", {}) or {}
        best = optimization.get("best_individual", {}) or {}
        score_components = best.get("score_components", {}) or {}
        full = summary.get("best_backtests", {}).get("full", {}).get("metrics", {}) or {}
        validation = summary.get("best_backtests", {}).get("validation", {}).get("metrics", {}) or {}
        train = summary.get("best_backtests", {}).get("train", {}).get("metrics", {}) or {}
        walk_forward = summary.get("best_backtests", {}).get("walk_forward", {}) or {}
        population = optimization.get("population_overview", {}) or {}
        strategy_meta = summary.get("strategy", {}).get("strategy_meta", {}) or {}
        strategy_structure = strategy_meta.get("strategy_structure", {}) if isinstance(strategy_meta.get("strategy_structure"), dict) else {}
        train_score = score_components.get("train_score")
        validation_score = score_components.get("validation_score")
        return {
            "attempt_id": summary.get("attempt_id"),
            "strategy_name": summary.get("strategy", {}).get("strategy_name"),
            "strategy_alpha": strategy_structure.get("alpha"),
            "strategy_filters_json": json.dumps(strategy_structure.get("filters") or [], ensure_ascii=False, sort_keys=True),
            "strategy_exit_policy_json": json.dumps(strategy_structure.get("exit_policy") or [], ensure_ascii=False, sort_keys=True),
            "strategy_position_mapper": strategy_structure.get("position_mapper"),
            "strategy_state_rules_json": json.dumps(strategy_structure.get("state_rules") or [], ensure_ascii=False, sort_keys=True),
            "status": summary.get("status"),
            "score": summary.get("score"),
            "search_method": best.get("search_method"),
            "best_params_json": json.dumps(optimization.get("best_params") or {}, ensure_ascii=False, sort_keys=True),
            "full_total_return": full.get("total_return"),
            "full_annual_return": full.get("annual_return"),
            "full_sharpe": full.get("sharpe"),
            "full_max_drawdown": full.get("max_drawdown"),
            "full_calmar": full.get("calmar"),
            "full_excess_total_return": full.get("excess_total_return"),
            "full_information_ratio": full.get("information_ratio"),
            "full_benchmark_total_return": full.get("benchmark_total_return"),
            "train_score": train_score,
            "validation_score": validation_score,
            "train_validation_gap": self._safe_diff(train_score, validation_score),
            "train_total_return": train.get("total_return"),
            "validation_total_return": validation.get("total_return"),
            "walk_forward_mean_score": score_components.get("walk_forward_mean_score") or walk_forward.get("mean_score"),
            "walk_forward_std_score": score_components.get("walk_forward_std_score") or walk_forward.get("std_score"),
            "walk_forward_min_score": score_components.get("walk_forward_min_score") or walk_forward.get("min_score"),
            "overfit_penalty": score_components.get("overfit_penalty"),
            "walk_forward_instability_penalty": score_components.get("walk_forward_instability_penalty"),
            "order_count": full.get("order_count"),
            "signal_changes": full.get("signal_changes"),
            "population_candidate_count": population.get("candidate_count"),
            "attempt_summary_path": self._string_path(optimization.get("attempt_summary_path")),
            "critic_inputs_count": len(summary.get("critic_inputs", [])),
        }

    def _format_markdown(self, summary: dict[str, Any]) -> str:
        lines = [
            "# Attempt Comparison",
            "",
            f"- Run ID: {summary.get('run_id')}",
            f"- Attempt Count: {summary.get('attempt_count')}",
            f"- Best Attempt ID: {summary.get('best_attempt_id')}",
            "",
            "## Ranking",
            "",
            "| rank | attempt | score | full_return | max_drawdown | excess | wf_mean | wf_std | wf_min | gap | orders |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in summary.get("ranking", []):
            lines.append(
                "| {rank} | {attempt} | {score} | {ret} | {dd} | {excess} | {wf_mean} | {wf_std} | {wf_min} | {gap} | {orders} |".format(
                    rank=row.get("rank"),
                    attempt=row.get("attempt_id"),
                    score=self._fmt(row.get("score")),
                    ret=self._fmt(row.get("full_total_return")),
                    dd=self._fmt(row.get("full_max_drawdown")),
                    excess=self._fmt(row.get("full_excess_total_return")),
                    wf_mean=self._fmt(row.get("walk_forward_mean_score")),
                    wf_std=self._fmt(row.get("walk_forward_std_score")),
                    wf_min=self._fmt(row.get("walk_forward_min_score")),
                    gap=self._fmt(row.get("train_validation_gap")),
                    orders=row.get("order_count"),
                )
            )
        lines.append("")
        lines.extend(["## Strategy Structure", ""])
        lines.append("| rank | attempt | alpha | filters | exit_policy | position_mapper | state_rules |")
        lines.append("| ---: | --- | --- | --- | --- | --- | --- |")
        for row in summary.get("ranking", []):
            lines.append(
                "| {rank} | {attempt} | {alpha} | {filters} | {exit_policy} | {mapper} | {state_rules} |".format(
                    rank=row.get("rank"),
                    attempt=row.get("attempt_id"),
                    alpha=row.get("strategy_alpha") or "",
                    filters=row.get("strategy_filters_json") or "",
                    exit_policy=row.get("strategy_exit_policy_json") or "",
                    mapper=row.get("strategy_position_mapper") or "",
                    state_rules=row.get("strategy_state_rules_json") or "",
                )
            )
        lines.append("")
        return "\n".join(lines)

    def _write_charts(self, *, df: pd.DataFrame, output_dir: Path) -> dict[str, Path]:
        try:
            import matplotlib

            matplotlib.use("Agg", force=True)
            import matplotlib.pyplot as plt
        except ImportError:
            return {}

        chart_paths: dict[str, Path] = {}
        plot_df = df.copy()
        plot_df["attempt_id"] = plot_df["attempt_id"].astype(str)

        score_path = output_dir / "score_ranking.png"
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.bar(plot_df["attempt_id"], pd.to_numeric(plot_df["score"], errors="coerce"), color="#2563eb")
        ax.set_title("Attempt Score Ranking", loc="left", fontweight="bold")
        ax.tick_params(axis="x", rotation=30)
        ax.grid(True, axis="y", alpha=0.25)
        fig.tight_layout()
        fig.savefig(score_path, dpi=160, bbox_inches="tight")
        plt.close(fig)
        chart_paths["score_ranking"] = score_path

        scatter_path = output_dir / "return_drawdown_scatter.png"
        fig, ax = plt.subplots(figsize=(8, 6))
        ax.scatter(plot_df["full_max_drawdown"], plot_df["full_total_return"], s=70, color="#16a34a")
        for _, row in plot_df.iterrows():
            ax.annotate(row["attempt_id"], (row["full_max_drawdown"], row["full_total_return"]), fontsize=8)
        ax.set_xlabel("Max Drawdown")
        ax.set_ylabel("Total Return")
        ax.set_title("Return vs Drawdown", loc="left", fontweight="bold")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(scatter_path, dpi=160, bbox_inches="tight")
        plt.close(fig)
        chart_paths["return_drawdown_scatter"] = scatter_path

        wf_path = output_dir / "walk_forward_stability.png"
        fig, ax = plt.subplots(figsize=(10, 5))
        x = range(len(plot_df))
        ax.bar(x, plot_df["walk_forward_mean_score"], color="#2563eb", label="Mean")
        ax.errorbar(x, plot_df["walk_forward_mean_score"], yerr=plot_df["walk_forward_std_score"], fmt="none", ecolor="#111827", capsize=4)
        ax.scatter(x, plot_df["walk_forward_min_score"], color="#dc2626", label="Min Fold", zorder=3)
        ax.set_xticks(list(x))
        ax.set_xticklabels(plot_df["attempt_id"], rotation=30, ha="right")
        ax.set_title("Walk-forward Stability", loc="left", fontweight="bold")
        ax.grid(True, axis="y", alpha=0.25)
        ax.legend(frameon=False)
        fig.tight_layout()
        fig.savefig(wf_path, dpi=160, bbox_inches="tight")
        plt.close(fig)
        chart_paths["walk_forward_stability"] = wf_path
        return chart_paths

    def _select_attempts(self, state: dict[str, Any], attempt_ids: list[str]) -> list[dict[str, Any]]:
        all_attempts = state.get("attempts", [])
        if not attempt_ids:
            selected = [
                item
                for item in all_attempts
                if item.get("attempt_summary_path") or item.get("status") in {"ready_for_review", "reviewed", "optimized"}
            ]
        else:
            wanted = set(attempt_ids)
            selected = [item for item in all_attempts if item.get("attempt_id") in wanted]
        missing = sorted(set(attempt_ids) - {item.get("attempt_id") for item in selected})
        if missing:
            raise ValueError(f"run_state.json 中不存在这些 attempt：{missing}")
        if not selected:
            raise ValueError("没有可比较的 attempt。")
        return selected

    def _resolve_output_dir(self, output_dir: Path | None, state: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        reports_dir = self._resolve_path(state.get("directories", {}).get("reports", "artifacts/signal_runs/reports"))
        return reports_dir / f"attempt_comparison_{timestamp}"

    def _safe_diff(self, left: Any, right: Any) -> float | None:
        try:
            return float(left) - float(right)
        except (TypeError, ValueError):
            return None

    def _sort_score(self, row: dict[str, Any]) -> float:
        try:
            return float(row.get("score"))
        except (TypeError, ValueError):
            return float("-inf")

    def _fmt(self, value: Any) -> str:
        if value is None:
            return ""
        try:
            return f"{float(value):.6g}"
        except (TypeError, ValueError):
            return str(value)

    def _string_path(self, value: Any) -> str | None:
        return None if value is None else str(value)

    def _resolve_optional(self, value: str | Path | None) -> Path | None:
        if not value:
            return None
        return self._resolve_path(value)

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
