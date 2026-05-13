from __future__ import annotations

import itertools
import hashlib
import json
import math
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.signal_backtest import SignalBacktestEvaluator, SignalBacktestRequest
from strategy_lab.services.signal_run import SignalRunManager


class ParameterSearchRequest(BaseModel):
    run_state_path: Path
    attempt_id: str
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


class ParameterSearchResult(BaseModel):
    run_state_path: Path
    attempt_id: str
    search_method: str
    optimization_dir: Path
    best_params: dict[str, Any]
    best_score: float
    best_individual_path: Path
    population_summary_path: Path
    search_result_path: Path
    full_backtest_dir: Path
    train_backtest_dir: Path
    validation_backtest_dir: Path
    walk_forward_summary_path: Path | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class ParameterSearchService:
    """参数搜索和稳健性评估服务。

    负责生成候选参数、调用回测、选择最佳个体，并把最佳个体跑到标准 backtests 目录。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)
        self.backtester = SignalBacktestEvaluator(config=self.config)

    def run(self, request: ParameterSearchRequest) -> ParameterSearchResult:
        rng = random.Random(request.random_seed)
        state = self.run_manager.load_state(request.run_state_path)
        attempt = self._find_attempt(state, request.attempt_id)
        split_manifest_path = self._resolve_split_manifest(request.data_split_manifest_path, state=state)
        split_manifest = json.loads(split_manifest_path.read_text(encoding="utf-8"))
        strategy_ref = attempt.get("strategy_ref")
        if not strategy_ref:
            raise ValueError(f"{request.attempt_id} 尚未保存策略产物，缺少 strategy_ref。")
        param_space_path = self._resolve_path(attempt["param_space_path"])
        param_space = json.loads(param_space_path.read_text(encoding="utf-8"))

        optimization_dir = self._resolve_path(attempt["optimization_dir"])
        candidate_backtests_dir = optimization_dir / "candidate_backtests"
        candidate_cache_dir = optimization_dir / "candidate_cache"
        candidate_backtests_dir.mkdir(parents=True, exist_ok=True)
        candidate_cache_dir.mkdir(parents=True, exist_ok=True)
        optimization_config_path = optimization_dir / "optimization_config.json"
        optimization_config_path.write_text(
            request.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )

        if request.search_method.lower() == "ga":
            population_rows = self._evaluate_ga(
                param_space=param_space,
                strategy_ref=strategy_ref,
                split_manifest=split_manifest,
                candidate_backtests_dir=candidate_backtests_dir,
                candidate_cache_dir=candidate_cache_dir,
                request=request,
                rng=rng,
            )
        else:
            candidates = self._build_candidates(
                param_space=param_space,
                request=request,
                rng=rng,
            )
            population_rows = self._evaluate_candidates(
                candidates=candidates,
                strategy_ref=strategy_ref,
                split_manifest=split_manifest,
                candidate_backtests_dir=candidate_backtests_dir,
                candidate_cache_dir=candidate_cache_dir,
                request=request,
            )
        if not population_rows:
            raise RuntimeError("没有任何候选参数完成评估。")

        population_df = pd.DataFrame(population_rows).sort_values("score", ascending=False).reset_index(drop=True)
        best_row = population_df.iloc[0].to_dict()
        best_params = json.loads(best_row["params_json"])
        best_score = float(best_row["score"])

        best_individual = {
            "attempt_id": request.attempt_id,
            "selected_at": datetime.now().isoformat(),
            "search_method": request.search_method,
            "score": best_score,
            "params": best_params,
            "score_components": {
                "train_score": best_row.get("train_score"),
                "validation_score": best_row.get("validation_score"),
                "walk_forward_mean_score": best_row.get("walk_forward_mean_score"),
                "walk_forward_std_score": best_row.get("walk_forward_std_score"),
                "walk_forward_min_score": best_row.get("walk_forward_min_score"),
                "overfit_penalty": best_row.get("overfit_penalty"),
                "walk_forward_instability_penalty": best_row.get("walk_forward_instability_penalty"),
            },
            "metrics": {
                "train": self._row_metrics(best_row, "train"),
                "validation": self._row_metrics(best_row, "validation"),
                "walk_forward": json.loads(best_row.get("walk_forward_scores_json") or "[]"),
            },
        }

        full_dir = self._resolve_path(attempt["full_backtest_dir"])
        train_dir = self._resolve_path(attempt["train_backtest_dir"])
        validation_dir = self._resolve_path(attempt["validation_backtest_dir"])
        walk_forward_dir = self._resolve_path(attempt["walk_forward_dir"])
        for directory in [full_dir, train_dir, validation_dir, walk_forward_dir]:
            directory.mkdir(parents=True, exist_ok=True)

        full_result = self._run_backtest(
            data_path=self._resolve_path(split_manifest["source_data_path"]),
            output_dir=full_dir,
            strategy_ref=strategy_ref,
            params=best_params,
            request=request,
            run_suffix="full",
        )
        train_result = self._run_backtest(
            data_path=self._resolve_path(split_manifest["train_path"]),
            output_dir=train_dir,
            strategy_ref=strategy_ref,
            params=best_params,
            request=request,
            run_suffix="train",
        )
        validation_result = self._run_backtest(
            data_path=self._resolve_path(split_manifest["validation_path"]),
            output_dir=validation_dir,
            strategy_ref=strategy_ref,
            params=best_params,
            request=request,
            run_suffix="validation",
        )
        walk_forward_summary = self._evaluate_walk_forward_best(
            split_manifest=split_manifest,
            walk_forward_dir=walk_forward_dir,
            strategy_ref=strategy_ref,
            params=best_params,
            request=request,
        )

        population_summary_path = optimization_dir / "population_summary.csv"
        population_json_path = optimization_dir / "population_summary.json"
        best_individual_path = optimization_dir / "best_individual.json"
        validation_summary_path = optimization_dir / "validation_summary.json"
        walk_forward_summary_path = optimization_dir / "walk_forward_summary.json"
        search_result_path = optimization_dir / "search_result.json"

        population_df.to_csv(population_summary_path, index=False, encoding="utf-8-sig")
        population_json_path.write_text(
            json.dumps(population_rows, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        best_individual_path.write_text(json.dumps(best_individual, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        validation_summary_path.write_text(
            json.dumps(
                {
                    "best_params": best_params,
                    "train_metrics": train_result.metrics,
                    "validation_metrics": validation_result.metrics,
                    "full_metrics": full_result.metrics,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        walk_forward_summary_path.write_text(json.dumps(walk_forward_summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        search_result = {
            "attempt_id": request.attempt_id,
            "search_method": request.search_method,
            "candidate_count": int(len(population_df)),
            "best_score": best_score,
            "best_params": best_params,
            "best_individual_path": str(self._relative(best_individual_path)),
            "population_summary_path": str(self._relative(population_summary_path)),
            "validation_summary_path": str(self._relative(validation_summary_path)),
            "walk_forward_summary_path": str(self._relative(walk_forward_summary_path)),
            "full_backtest_dir": str(self._relative(full_dir)),
            "train_backtest_dir": str(self._relative(train_dir)),
            "validation_backtest_dir": str(self._relative(validation_dir)),
        }
        search_result_path.write_text(json.dumps(search_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        self.run_manager.update_attempt(
            request.run_state_path,
            attempt_id=request.attempt_id,
            status="optimized",
            score=best_score,
            fields={
                "best_params": best_params,
                "best_individual_path": str(self._relative(best_individual_path)),
                "population_summary_path": str(self._relative(population_summary_path)),
                "population_summary_json_path": str(self._relative(population_json_path)),
                "validation_summary_path": str(self._relative(validation_summary_path)),
                "walk_forward_summary_path": str(self._relative(walk_forward_summary_path)),
                "search_result_path": str(self._relative(search_result_path)),
                "full_metrics_path": str(self._relative(full_dir / "metrics.json")),
                "train_metrics_path": str(self._relative(train_dir / "metrics.json")),
                "validation_metrics_path": str(self._relative(validation_dir / "metrics.json")),
            },
        )
        latest_state = self.run_manager.load_state(request.run_state_path)
        current_best_score = latest_state.get("steps", {}).get("strategy_search", {}).get("best_score")
        should_replace_best = current_best_score is None or best_score > self._number(current_best_score)
        self.run_manager.update_strategy_search(
            request.run_state_path,
            status="running",
            current_attempt=request.attempt_id,
            best_attempt_id=request.attempt_id if should_replace_best else None,
            best_score=best_score if should_replace_best else None,
        )
        self.run_manager.append_event(
            request.run_state_path,
            actor="ParameterSearchService",
            event="parameter_search_completed",
            summary=f"{request.attempt_id} 完成参数搜索，最佳得分 {best_score:.6f}。",
            extra={"attempt_id": request.attempt_id, "search_result_path": str(self._relative(search_result_path))},
        )

        return ParameterSearchResult(
            run_state_path=self._resolve_path(request.run_state_path),
            attempt_id=request.attempt_id,
            search_method=request.search_method,
            optimization_dir=optimization_dir,
            best_params=best_params,
            best_score=best_score,
            best_individual_path=best_individual_path,
            population_summary_path=population_summary_path,
            search_result_path=search_result_path,
            full_backtest_dir=full_dir,
            train_backtest_dir=train_dir,
            validation_backtest_dir=validation_dir,
            walk_forward_summary_path=walk_forward_summary_path,
            summary=search_result,
        )

    def _build_candidates(
        self,
        *,
        param_space: dict[str, Any],
        request: ParameterSearchRequest,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        method = request.search_method.lower()
        values = {name: self._values_from_spec(spec) for name, spec in param_space.items()}
        if not values:
            return [{}]
        if method == "grid":
            keys = list(values)
            grid = [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(values[key] for key in keys))]
            return grid[: request.max_candidates]
        if method == "random":
            return [self._random_candidate(values, rng) for _ in range(request.max_candidates)]
        if method == "ga":
            return [self._random_candidate(values, rng) for _ in range(min(request.population_size, request.max_candidates))]
        raise ValueError("search_method 必须是 grid、random 或 ga。")

    def _evaluate_ga(
        self,
        *,
        param_space: dict[str, Any],
        strategy_ref: str,
        split_manifest: dict[str, Any],
        candidate_backtests_dir: Path,
        candidate_cache_dir: Path,
        request: ParameterSearchRequest,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        values = {name: self._values_from_spec(spec) for name, spec in param_space.items()}
        if not values:
            return self._evaluate_candidates(
                candidates=[{}],
                strategy_ref=strategy_ref,
                split_manifest=split_manifest,
                candidate_backtests_dir=candidate_backtests_dir,
                candidate_cache_dir=candidate_cache_dir,
                request=request,
            )
        population = [self._random_candidate(values, rng) for _ in range(max(2, request.population_size))]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidate_index = 1
        best_score = -math.inf
        stale_generations = 0
        for generation in range(1, max(1, request.generations) + 1):
            generation_rows: list[dict[str, Any]] = []
            generation_candidates: list[tuple[str, dict[str, Any]]] = []
            for candidate in population:
                if len(rows) + len(generation_candidates) >= request.max_candidates:
                    break
                key = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                generation_candidates.append((f"candidate_{candidate_index:03d}", candidate))
                candidate_index += 1
            if generation_candidates:
                generation_rows = self._evaluate_candidate_batch(
                    candidate_items=generation_candidates,
                    strategy_ref=strategy_ref,
                    split_manifest=split_manifest,
                    candidate_backtests_dir=candidate_backtests_dir,
                    candidate_cache_dir=candidate_cache_dir,
                    request=request,
                )
                for row in generation_rows:
                    row["generation"] = generation
                rows.extend(generation_rows)
                current_best = max(float(row["score"]) for row in generation_rows)
                if current_best > best_score + request.min_improvement:
                    best_score = current_best
                    stale_generations = 0
                else:
                    stale_generations += 1
                if len(rows) >= request.max_candidates or stale_generations >= max(1, request.ga_patience):
                    return rows[: request.max_candidates]
            if not generation_rows:
                population = [self._random_candidate(values, rng) for _ in range(max(2, request.population_size))]
                continue
            generation_rows = sorted(generation_rows, key=lambda item: float(item["score"]), reverse=True)
            elite_rows = generation_rows[: max(2, len(generation_rows) // 2)]
            elites = [json.loads(row["params_json"]) for row in elite_rows]
            next_population = elites.copy()
            while len(next_population) < len(population):
                parent_a = rng.choice(elites)
                parent_b = rng.choice(elites)
                child = {}
                for name, choices in values.items():
                    child[name] = parent_a[name] if rng.random() < 0.5 else parent_b[name]
                    if rng.random() < request.mutation_rate:
                        child[name] = rng.choice(choices)
                next_population.append(child)
            population = next_population
        return rows[: request.max_candidates]

    def _evaluate_candidates(
        self,
        *,
        candidates: list[dict[str, Any]],
        strategy_ref: str,
        split_manifest: dict[str, Any],
        candidate_backtests_dir: Path,
        candidate_cache_dir: Path,
        request: ParameterSearchRequest,
    ) -> list[dict[str, Any]]:
        candidate_items = [(f"candidate_{index:03d}", params) for index, params in enumerate(candidates[: request.max_candidates], start=1)]
        return self._evaluate_candidate_batch(
            candidate_items=candidate_items,
            strategy_ref=strategy_ref,
            split_manifest=split_manifest,
            candidate_backtests_dir=candidate_backtests_dir,
            candidate_cache_dir=candidate_cache_dir,
            request=request,
        )

    def _evaluate_candidate_batch(
        self,
        *,
        candidate_items: list[tuple[str, dict[str, Any]]],
        strategy_ref: str,
        split_manifest: dict[str, Any],
        candidate_backtests_dir: Path,
        candidate_cache_dir: Path,
        request: ParameterSearchRequest,
    ) -> list[dict[str, Any]]:
        if request.max_workers <= 1 or len(candidate_items) <= 1:
            return [
                self._evaluate_candidate(
                    candidate_id=candidate_id,
                    params=params,
                    strategy_ref=strategy_ref,
                    split_manifest=split_manifest,
                    candidate_backtests_dir=candidate_backtests_dir,
                    candidate_cache_dir=candidate_cache_dir,
                    request=request,
                )
                for candidate_id, params in candidate_items
            ]

        rows_by_id: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, request.max_workers)) as executor:
            futures = {
                executor.submit(
                    self._evaluate_candidate,
                    candidate_id=candidate_id,
                    params=params,
                    strategy_ref=strategy_ref,
                    split_manifest=split_manifest,
                    candidate_backtests_dir=candidate_backtests_dir,
                    candidate_cache_dir=candidate_cache_dir,
                    request=request,
                ): candidate_id
                for candidate_id, params in candidate_items
            }
            for future in as_completed(futures):
                candidate_id = futures[future]
                rows_by_id[candidate_id] = future.result()
        return [rows_by_id[candidate_id] for candidate_id, _ in candidate_items if candidate_id in rows_by_id]

    def _evaluate_candidate(
        self,
        *,
        candidate_id: str,
        params: dict[str, Any],
        strategy_ref: str,
        split_manifest: dict[str, Any],
        candidate_backtests_dir: Path,
        candidate_cache_dir: Path,
        request: ParameterSearchRequest,
    ) -> dict[str, Any]:
        cache_key = self._candidate_cache_key(strategy_ref=strategy_ref, split_manifest=split_manifest, params=params)
        cache_path = candidate_cache_dir / f"{cache_key}.json"
        if request.cache_enabled and cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["candidate_id"] = candidate_id
            cached["cache_hit"] = True
            cached["cache_path"] = str(self._relative(cache_path))
            return cached

        candidate_dir = candidate_backtests_dir / candidate_id
        train_result = self._run_backtest(
            data_path=self._resolve_path(split_manifest["train_path"]),
            output_dir=candidate_dir / "train",
            strategy_ref=strategy_ref,
            params=params,
            request=request,
            run_suffix=f"{candidate_id}_train",
        )
        validation_result = self._run_backtest(
            data_path=self._resolve_path(split_manifest["validation_path"]),
            output_dir=candidate_dir / "validation",
            strategy_ref=strategy_ref,
            params=params,
            request=request,
            run_suffix=f"{candidate_id}_validation",
        )
        walk_forward_rows = self._evaluate_walk_forward_candidate(
            split_manifest=split_manifest,
            candidate_dir=candidate_dir,
            strategy_ref=strategy_ref,
            params=params,
            request=request,
            candidate_id=candidate_id,
        )
        train_score = self._metric_score(train_result.metrics)
        validation_score = self._metric_score(validation_result.metrics)
        walk_forward_scores = [float(item["score"]) for item in walk_forward_rows]
        walk_forward_mean_score = self._mean(walk_forward_scores)
        walk_forward_std_score = self._std(walk_forward_scores)
        walk_forward_min_score = min(walk_forward_scores) if walk_forward_scores else 0.0
        overfit_penalty = max(0.0, train_score - validation_score) * 0.25
        walk_forward_instability_penalty = 0.30 * walk_forward_std_score + 0.50 * max(0.0, -walk_forward_min_score)
        score = (
            0.20 * train_score
            + 0.35 * validation_score
            + 0.45 * walk_forward_mean_score
            - overfit_penalty
            - walk_forward_instability_penalty
        )
        row = {
            "candidate_id": candidate_id,
            "params_json": json.dumps(params, ensure_ascii=False, sort_keys=True),
            "score": score,
            "train_score": train_score,
            "validation_score": validation_score,
            "walk_forward_mean_score": walk_forward_mean_score,
            "walk_forward_std_score": walk_forward_std_score,
            "walk_forward_min_score": walk_forward_min_score,
            "walk_forward_max_score": max(walk_forward_scores) if walk_forward_scores else 0.0,
            "overfit_penalty": overfit_penalty,
            "walk_forward_instability_penalty": walk_forward_instability_penalty,
            "walk_forward_scores_json": json.dumps(walk_forward_rows, ensure_ascii=False, default=str),
            "cache_hit": False,
            "cache_path": str(self._relative(cache_path)),
            **self._flatten_metrics(train_result.metrics, prefix="train"),
            **self._flatten_metrics(validation_result.metrics, prefix="validation"),
            "train_output_dir": str(self._relative(train_result.output_dir)),
            "validation_output_dir": str(self._relative(validation_result.output_dir)),
        }
        if request.cache_enabled:
            cache_path.write_text(json.dumps(row, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return row

    def _evaluate_walk_forward_best(
        self,
        *,
        split_manifest: dict[str, Any],
        walk_forward_dir: Path,
        strategy_ref: str,
        params: dict[str, Any],
        request: ParameterSearchRequest,
    ) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for fold in split_manifest.get("folds", []):
            fold_id = fold["fold_id"]
            fold_dir = walk_forward_dir / fold_id
            result = self._run_backtest(
                data_path=self._resolve_path(fold.get("context_path") or fold["validation_path"]),
                output_dir=fold_dir,
                strategy_ref=strategy_ref,
                params=params,
                request=request,
                run_suffix=f"{fold_id}_validation",
                evaluation_start=fold.get("evaluation_start") or fold.get("validation_start"),
                evaluation_end=fold.get("evaluation_end") or fold.get("validation_end"),
            )
            rows.append(
                {
                    "fold_id": fold_id,
                    "validation_path": fold["validation_path"],
                    "output_dir": str(self._relative(result.output_dir)),
                    "score": self._metric_score(result.metrics),
                    **self._flatten_metrics(result.metrics, prefix="metrics"),
                }
            )
        scores = [float(row["score"]) for row in rows]
        return {
            "fold_count": len(rows),
            "mean_score": float(sum(scores) / len(scores)) if scores else None,
            "std_score": self._std(scores),
            "min_score": float(min(scores)) if scores else None,
            "max_score": float(max(scores)) if scores else None,
            "folds": rows,
        }

    def _evaluate_walk_forward_candidate(
        self,
        *,
        split_manifest: dict[str, Any],
        candidate_dir: Path,
        strategy_ref: str,
        params: dict[str, Any],
        request: ParameterSearchRequest,
        candidate_id: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for fold in split_manifest.get("folds", []):
            fold_id = fold["fold_id"]
            result = self._run_backtest(
                data_path=self._resolve_path(fold.get("context_path") or fold["validation_path"]),
                output_dir=candidate_dir / "walk_forward" / fold_id,
                strategy_ref=strategy_ref,
                params=params,
                request=request,
                run_suffix=f"{candidate_id}_{fold_id}",
                evaluation_start=fold.get("evaluation_start") or fold.get("validation_start"),
                evaluation_end=fold.get("evaluation_end") or fold.get("validation_end"),
            )
            rows.append(
                {
                    "fold_id": fold_id,
                    "context_path": fold.get("context_path"),
                    "validation_path": fold["validation_path"],
                    "evaluation_start": fold.get("evaluation_start") or fold.get("validation_start"),
                    "evaluation_end": fold.get("evaluation_end") or fold.get("validation_end"),
                    "output_dir": str(self._relative(result.output_dir)),
                    "score": self._metric_score(result.metrics),
                    **self._flatten_metrics(result.metrics, prefix="metrics"),
                }
            )
        return rows

    def _run_backtest(
        self,
        *,
        data_path: Path,
        output_dir: Path,
        strategy_ref: str,
        params: dict[str, Any],
        request: ParameterSearchRequest,
        run_suffix: str,
        evaluation_start: str | None = None,
        evaluation_end: str | None = None,
    ):
        return SignalBacktestEvaluator(config=self.config).run(
            SignalBacktestRequest(
                data_path=data_path,
                output_dir=output_dir,
                run_state_path=request.run_state_path,
                strategy=strategy_ref,
                strategy_params=params,
                run_id=f"{request.attempt_id}_{run_suffix}",
                quantstats_html=request.quantstats_html,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
            )
        )

    def _metric_score(self, metrics: dict[str, Any]) -> float:
        sharpe = self._number(metrics.get("sharpe"))
        calmar = self._number(metrics.get("calmar"))
        total_return = self._number(metrics.get("total_return"))
        max_drawdown = abs(self._number(metrics.get("max_drawdown")))
        excess_return = self._number(metrics.get("excess_total_return"))
        return 2.00 * sharpe + 0.50 * calmar + 0.80 * total_return + 0.50 * excess_return - 0.80 * max_drawdown

    def _candidate_cache_key(
        self,
        *,
        strategy_ref: str,
        split_manifest: dict[str, Any],
        params: dict[str, Any],
    ) -> str:
        payload = {
            "score_formula_version": "metric_score_v2_sharpe_weight_2_20260508",
            "strategy_ref": strategy_ref,
            "params": params,
            "source_data_path": split_manifest.get("source_data_path"),
            "train_path": split_manifest.get("train_path"),
            "validation_path": split_manifest.get("validation_path"),
            "folds": [
                {
                    "context_path": fold.get("context_path"),
                    "validation_path": fold.get("validation_path"),
                    "evaluation_start": fold.get("evaluation_start") or fold.get("validation_start"),
                    "evaluation_end": fold.get("evaluation_end") or fold.get("validation_end"),
                }
                for fold in split_manifest.get("folds", [])
            ],
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

    def _mean(self, values: list[float]) -> float:
        return float(sum(values) / len(values)) if values else 0.0

    def _std(self, values: list[float]) -> float:
        if len(values) <= 1:
            return 0.0
        mean = self._mean(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return float(math.sqrt(variance))

    def _flatten_metrics(self, metrics: dict[str, Any], *, prefix: str) -> dict[str, Any]:
        keys = [
            "total_return",
            "annual_return",
            "sharpe",
            "max_drawdown",
            "calmar",
            "win_rate",
            "excess_total_return",
            "benchmark_total_return",
            "order_count",
            "signal_changes",
        ]
        return {f"{prefix}_{key}": metrics.get(key) for key in keys}

    def _row_metrics(self, row: dict[str, Any], prefix: str) -> dict[str, Any]:
        return {key.removeprefix(f"{prefix}_"): value for key, value in row.items() if key.startswith(f"{prefix}_")}

    def _values_from_spec(self, spec: Any) -> list[Any]:
        if not isinstance(spec, dict):
            return [spec]
        if "values" in spec:
            values = list(spec["values"])
        else:
            value_type = str(spec.get("type", "float")).lower()
            low = spec.get("low", spec.get("min"))
            high = spec.get("high", spec.get("max"))
            step = spec.get("step")
            if low is None or high is None:
                values = [spec.get("default")]
            elif value_type == "int":
                actual_step = int(step or max(1, round((int(high) - int(low)) / 6)))
                values = list(range(int(low), int(high) + 1, actual_step))
            elif value_type == "bool":
                values = [False, True]
            else:
                actual_step = float(step) if step is not None else None
                if actual_step and actual_step > 0:
                    count = int(math.floor((float(high) - float(low)) / actual_step)) + 1
                    values = [round(float(low) + i * actual_step, 10) for i in range(count)]
                else:
                    points = 7
                    values = [round(float(low) + (float(high) - float(low)) * i / (points - 1), 10) for i in range(points)]
        return [value for value in values if value is not None]

    def _random_candidate(self, values: dict[str, list[Any]], rng: random.Random) -> dict[str, Any]:
        return {name: rng.choice(choices) for name, choices in values.items()}

    def _resolve_split_manifest(self, data_split_manifest_path: Path | None, *, state: dict[str, Any]) -> Path:
        if data_split_manifest_path is not None:
            return self._resolve_path(data_split_manifest_path)
        manifest_path = state.get("artifacts", {}).get("datasets", {}).get("splits", {}).get("manifest_path")
        if not manifest_path:
            raise ValueError("未传 data_split_manifest_path，且 run_state.json 中没有 artifacts.datasets.splits.manifest_path。")
        return self._resolve_path(manifest_path)

    def _find_attempt(self, state: dict[str, Any], attempt_id: str) -> dict[str, Any]:
        attempt = next((item for item in state.get("attempts", []) if item.get("attempt_id") == attempt_id), None)
        if attempt is None:
            raise ValueError(f"run_state.json 中不存在 attempt：{attempt_id}")
        return attempt

    def _number(self, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if math.isnan(number) or math.isinf(number):
            return 0.0
        return number

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
