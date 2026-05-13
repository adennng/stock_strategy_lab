from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_backtest import BudgetBacktestRequest, BudgetBacktestService
from strategy_lab.services.budget_policy_engine import BudgetPolicyEngine, BudgetPolicyEngineRequest
from strategy_lab.services.budget_run import BudgetRunManager


class BudgetParameterSearchRequest(BaseModel):
    budget_run_state_path: Path
    policy_config_path: Path
    param_space_path: Path
    output_dir: Path | None = None
    search_id: str | None = None
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
    update_run_state: bool = True
    generate_chart: bool = False


class BudgetParameterSearchResult(BaseModel):
    budget_run_state_path: Path
    search_id: str
    output_dir: Path
    best_params: dict[str, Any]
    best_score: float
    best_policy_config_path: Path
    best_execution_manifest_path: Path
    best_backtest_manifest_path: Path
    population_summary_path: Path
    search_result_path: Path
    walk_forward_summary_path: Path | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class BudgetParameterSearchService:
    """预算层参数搜索服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetParameterSearchRequest) -> BudgetParameterSearchResult:
        rng = random.Random(request.random_seed)
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        base_config_path = self._resolve_path(request.policy_config_path)
        param_space_path = self._resolve_path(request.param_space_path)
        base_config = json.loads(base_config_path.read_text(encoding="utf-8"))
        param_space_raw = json.loads(param_space_path.read_text(encoding="utf-8"))
        param_space = self._normalize_param_space(param_space_raw)
        split_manifest_path = self._resolve_split_manifest(request.data_split_manifest_path, state=state)
        split_manifest = json.loads(split_manifest_path.read_text(encoding="utf-8"))
        search_id = request.search_id or self._make_search_id(base_config)
        output_dir = self._resolve_output_dir(request.output_dir, state=state, search_id=search_id)
        candidates_dir = output_dir / "candidates"
        cache_dir = output_dir / "candidate_cache"
        best_dir = output_dir / "best"
        for directory in [output_dir, candidates_dir, cache_dir, best_dir]:
            directory.mkdir(parents=True, exist_ok=True)
        (output_dir / "search_request.json").write_text(
            request.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )

        if request.search_method.lower() == "ga":
            rows = self._evaluate_ga(
                base_config=base_config,
                param_space=param_space,
                split_manifest=split_manifest,
                candidates_dir=candidates_dir,
                cache_dir=cache_dir,
                request=request,
                rng=rng,
            )
        else:
            candidates = self._build_candidates(param_space=param_space, request=request, rng=rng)
            rows = self._evaluate_candidates(
                candidates=candidates,
                base_config=base_config,
                split_manifest=split_manifest,
                candidates_dir=candidates_dir,
                cache_dir=cache_dir,
                request=request,
            )
        if not rows:
            raise RuntimeError("没有任何预算参数候选完成评估。")

        population_df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
        best_row = population_df.iloc[0].to_dict()
        best_params = json.loads(best_row["params_json"])
        best_config = self._apply_params(base_config, best_params)
        best_config_path = best_dir / "budget_policy_config.json"
        best_config_path.write_text(json.dumps(best_config, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        shutil.copy2(param_space_path, best_dir / "param_space.json")
        full_result = self._run_execution_and_backtest(
            policy_config_path=best_config_path,
            run_state_path=state_path,
            panel_path=self._resolve_path(split_manifest["source_panel_ohlcv_path"]),
            returns_path=self._resolve_path(split_manifest["source_returns_wide_path"]),
            output_dir=best_dir / "full",
            policy_id=f"{search_id}_best_full",
            backtest_id=f"{search_id}_best_full",
            request=request,
            update_run_state=False,
            generate_chart=True,
        )
        train_result = self._run_execution_and_backtest(
            policy_config_path=best_config_path,
            run_state_path=state_path,
            panel_path=self._resolve_path(split_manifest["train_panel_path"]),
            returns_path=self._resolve_path(split_manifest["train_returns_path"]),
            output_dir=best_dir / "train",
            policy_id=f"{search_id}_best_train",
            backtest_id=f"{search_id}_best_train",
            request=request,
            update_run_state=False,
        )
        validation_result = self._run_execution_and_backtest(
            policy_config_path=best_config_path,
            run_state_path=state_path,
            panel_path=self._resolve_path(split_manifest["validation_panel_path"]),
            returns_path=self._resolve_path(split_manifest["validation_returns_path"]),
            output_dir=best_dir / "validation",
            policy_id=f"{search_id}_best_validation",
            backtest_id=f"{search_id}_best_validation",
            request=request,
            update_run_state=False,
        )
        walk_forward_summary = self._evaluate_walk_forward_best(
            policy_config_path=best_config_path,
            split_manifest=split_manifest,
            output_dir=best_dir / "walk_forward",
            search_id=search_id,
            request=request,
            run_state_path=state_path,
        )

        population_summary_path = output_dir / "population_summary.csv"
        population_json_path = output_dir / "population_summary.json"
        best_individual_path = output_dir / "best_individual.json"
        walk_forward_summary_path = output_dir / "walk_forward_summary.json"
        search_result_path = output_dir / "search_result.json"
        population_df.to_csv(population_summary_path, index=False, encoding="utf-8-sig")
        population_json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        best_individual = {
            "search_id": search_id,
            "selected_at": datetime.now().isoformat(),
            "search_method": request.search_method,
            "score": float(best_row["score"]),
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
            "full_metrics": full_result["metrics"],
            "train_metrics": train_result["metrics"],
            "validation_metrics": validation_result["metrics"],
        }
        best_individual_path.write_text(json.dumps(best_individual, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        walk_forward_summary_path.write_text(json.dumps(walk_forward_summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        search_result = {
            "search_id": search_id,
            "search_method": request.search_method,
            "candidate_count": int(len(population_df)),
            "best_score": float(best_row["score"]),
            "best_params": best_params,
            "best_policy_config_path": str(self._relative(best_config_path)),
            "best_execution_manifest_path": str(self._relative(full_result["execution_manifest_path"])),
            "best_backtest_manifest_path": str(self._relative(full_result["backtest_manifest_path"])),
            "population_summary_path": str(self._relative(population_summary_path)),
            "population_json_path": str(self._relative(population_json_path)),
            "best_individual_path": str(self._relative(best_individual_path)),
            "walk_forward_summary_path": str(self._relative(walk_forward_summary_path)),
            "full_output_dir": str(self._relative(full_result["output_dir"])),
            "train_output_dir": str(self._relative(train_result["output_dir"])),
            "validation_output_dir": str(self._relative(validation_result["output_dir"])),
            "summary": {
                "full_total_return": full_result["metrics"].get("total_return"),
                "full_sharpe": full_result["metrics"].get("sharpe"),
                "full_max_drawdown": full_result["metrics"].get("max_drawdown"),
                "validation_total_return": validation_result["metrics"].get("total_return"),
                "validation_sharpe": validation_result["metrics"].get("sharpe"),
                "walk_forward_mean_score": walk_forward_summary.get("mean_score"),
            },
        }
        search_result_path.write_text(json.dumps(search_result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                search_id=search_id,
                search_result=search_result,
                search_result_path=search_result_path,
            )

        return BudgetParameterSearchResult(
            budget_run_state_path=state_path,
            search_id=search_id,
            output_dir=output_dir,
            best_params=best_params,
            best_score=float(best_row["score"]),
            best_policy_config_path=best_config_path,
            best_execution_manifest_path=full_result["execution_manifest_path"],
            best_backtest_manifest_path=full_result["backtest_manifest_path"],
            population_summary_path=population_summary_path,
            search_result_path=search_result_path,
            walk_forward_summary_path=walk_forward_summary_path,
            summary=search_result["summary"],
        )

    def _evaluate_ga(
        self,
        *,
        base_config: dict[str, Any],
        param_space: dict[str, list[Any]],
        split_manifest: dict[str, Any],
        candidates_dir: Path,
        cache_dir: Path,
        request: BudgetParameterSearchRequest,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        if not param_space:
            return self._evaluate_candidates(
                candidates=[{}],
                base_config=base_config,
                split_manifest=split_manifest,
                candidates_dir=candidates_dir,
                cache_dir=cache_dir,
                request=request,
            )
        population = [self._random_candidate(param_space, rng) for _ in range(max(2, request.population_size))]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidate_index = 1
        best_score = -math.inf
        stale_generations = 0
        for generation in range(1, max(1, request.generations) + 1):
            items: list[tuple[str, dict[str, Any]]] = []
            for candidate in population:
                if len(rows) + len(items) >= request.max_candidates:
                    break
                key = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                items.append((f"candidate_{candidate_index:03d}", candidate))
                candidate_index += 1
            generation_rows = self._evaluate_candidate_batch(
                candidate_items=items,
                base_config=base_config,
                split_manifest=split_manifest,
                candidates_dir=candidates_dir,
                cache_dir=cache_dir,
                request=request,
            ) if items else []
            for row in generation_rows:
                row["generation"] = generation
            rows.extend(generation_rows)
            if generation_rows:
                current_best = max(float(row["score"]) for row in generation_rows)
                if current_best > best_score + request.min_improvement:
                    best_score = current_best
                    stale_generations = 0
                else:
                    stale_generations += 1
                if len(rows) >= request.max_candidates or stale_generations >= max(1, request.ga_patience):
                    return rows[: request.max_candidates]
                elites = [json.loads(row["params_json"]) for row in sorted(generation_rows, key=lambda item: float(item["score"]), reverse=True)[: max(2, len(generation_rows) // 2)]]
            else:
                elites = [self._random_candidate(param_space, rng) for _ in range(max(2, request.population_size))]
            next_population = elites.copy()
            while len(next_population) < len(population):
                parent_a = rng.choice(elites)
                parent_b = rng.choice(elites)
                child = {}
                for name, choices in param_space.items():
                    child[name] = parent_a.get(name, rng.choice(choices)) if rng.random() < 0.5 else parent_b.get(name, rng.choice(choices))
                    if rng.random() < request.mutation_rate:
                        child[name] = rng.choice(choices)
                next_population.append(child)
            population = next_population
        return rows[: request.max_candidates]

    def _build_candidates(self, *, param_space: dict[str, list[Any]], request: BudgetParameterSearchRequest, rng: random.Random) -> list[dict[str, Any]]:
        method = request.search_method.lower()
        if not param_space:
            return [{}]
        if method == "grid":
            keys = list(param_space)
            grid = [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(param_space[key] for key in keys))]
            return grid[: request.max_candidates]
        if method == "random":
            return [self._random_candidate(param_space, rng) for _ in range(request.max_candidates)]
        if method == "ga":
            return [self._random_candidate(param_space, rng) for _ in range(min(request.population_size, request.max_candidates))]
        raise ValueError("search_method 必须是 grid、random 或 ga。")

    def _evaluate_candidates(
        self,
        *,
        candidates: list[dict[str, Any]],
        base_config: dict[str, Any],
        split_manifest: dict[str, Any],
        candidates_dir: Path,
        cache_dir: Path,
        request: BudgetParameterSearchRequest,
    ) -> list[dict[str, Any]]:
        items = [(f"candidate_{index:03d}", params) for index, params in enumerate(candidates[: request.max_candidates], start=1)]
        return self._evaluate_candidate_batch(
            candidate_items=items,
            base_config=base_config,
            split_manifest=split_manifest,
            candidates_dir=candidates_dir,
            cache_dir=cache_dir,
            request=request,
        )

    def _evaluate_candidate_batch(
        self,
        *,
        candidate_items: list[tuple[str, dict[str, Any]]],
        base_config: dict[str, Any],
        split_manifest: dict[str, Any],
        candidates_dir: Path,
        cache_dir: Path,
        request: BudgetParameterSearchRequest,
    ) -> list[dict[str, Any]]:
        if request.max_workers <= 1 or len(candidate_items) <= 1:
            return [
                self._evaluate_candidate(
                    candidate_id=candidate_id,
                    params=params,
                    base_config=base_config,
                    split_manifest=split_manifest,
                    candidates_dir=candidates_dir,
                    cache_dir=cache_dir,
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
                    base_config=base_config,
                    split_manifest=split_manifest,
                    candidates_dir=candidates_dir,
                    cache_dir=cache_dir,
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
        base_config: dict[str, Any],
        split_manifest: dict[str, Any],
        candidates_dir: Path,
        cache_dir: Path,
        request: BudgetParameterSearchRequest,
    ) -> dict[str, Any]:
        cache_key = self._candidate_cache_key(base_config=base_config, split_manifest=split_manifest, params=params)
        cache_path = cache_dir / f"{cache_key}.json"
        if request.cache_enabled and cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["candidate_id"] = candidate_id
            cached["cache_hit"] = True
            cached["cache_path"] = str(self._relative(cache_path))
            return cached

        candidate_dir = candidates_dir / candidate_id
        config = self._apply_params(base_config, params)
        config_path = candidate_dir / "budget_policy_config.json"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        train = self._run_execution_and_backtest(
            policy_config_path=config_path,
            run_state_path=request.budget_run_state_path,
            panel_path=self._resolve_path(split_manifest["train_panel_path"]),
            returns_path=self._resolve_path(split_manifest["train_returns_path"]),
            output_dir=candidate_dir / "train",
            policy_id=f"{candidate_id}_train",
            backtest_id=f"{candidate_id}_train",
            request=request,
            update_run_state=False,
        )
        validation = self._run_execution_and_backtest(
            policy_config_path=config_path,
            run_state_path=request.budget_run_state_path,
            panel_path=self._resolve_path(split_manifest["validation_panel_path"]),
            returns_path=self._resolve_path(split_manifest["validation_returns_path"]),
            output_dir=candidate_dir / "validation",
            policy_id=f"{candidate_id}_validation",
            backtest_id=f"{candidate_id}_validation",
            request=request,
            update_run_state=False,
        )
        walk_forward_rows = self._evaluate_walk_forward_candidate(
            policy_config_path=config_path,
            split_manifest=split_manifest,
            candidate_dir=candidate_dir,
            candidate_id=candidate_id,
            request=request,
            run_state_path=request.budget_run_state_path,
        )
        train_score = self._metric_score(train["metrics"])
        validation_score = self._metric_score(validation["metrics"])
        wf_scores = [float(item["score"]) for item in walk_forward_rows]
        wf_mean = self._mean(wf_scores)
        wf_std = self._std(wf_scores)
        wf_min = min(wf_scores) if wf_scores else 0.0
        overfit_penalty = max(0.0, train_score - validation_score) * 0.25
        wf_penalty = 0.30 * wf_std + 0.50 * max(0.0, -wf_min)
        score = 0.20 * train_score + 0.35 * validation_score + 0.45 * wf_mean - overfit_penalty - wf_penalty
        row = {
            "candidate_id": candidate_id,
            "params_json": json.dumps(params, ensure_ascii=False, sort_keys=True),
            "score": score,
            "train_score": train_score,
            "validation_score": validation_score,
            "walk_forward_mean_score": wf_mean,
            "walk_forward_std_score": wf_std,
            "walk_forward_min_score": wf_min,
            "walk_forward_max_score": max(wf_scores) if wf_scores else 0.0,
            "overfit_penalty": overfit_penalty,
            "walk_forward_instability_penalty": wf_penalty,
            "walk_forward_scores_json": json.dumps(walk_forward_rows, ensure_ascii=False, default=str),
            "cache_hit": False,
            "cache_path": str(self._relative(cache_path)),
            **self._flatten_metrics(train["metrics"], prefix="train"),
            **self._flatten_metrics(validation["metrics"], prefix="validation"),
            "train_output_dir": str(self._relative(train["output_dir"])),
            "validation_output_dir": str(self._relative(validation["output_dir"])),
        }
        if request.cache_enabled:
            cache_path.write_text(json.dumps(row, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return row

    def _evaluate_walk_forward_candidate(
        self,
        *,
        policy_config_path: Path,
        split_manifest: dict[str, Any],
        candidate_dir: Path,
        candidate_id: str,
        request: BudgetParameterSearchRequest,
        run_state_path: Path,
    ) -> list[dict[str, Any]]:
        rows = []
        for fold in split_manifest.get("folds", []):
            fold_id = fold["fold_id"]
            result = self._run_execution_and_backtest(
                policy_config_path=policy_config_path,
                run_state_path=run_state_path,
                panel_path=self._resolve_path(fold.get("context_panel_path") or fold["validation_panel_path"]),
                returns_path=self._resolve_path(fold.get("context_returns_path") or fold["validation_returns_path"]),
                output_dir=candidate_dir / "walk_forward" / fold_id,
                policy_id=f"{candidate_id}_{fold_id}",
                backtest_id=f"{candidate_id}_{fold_id}",
                request=request,
                update_run_state=False,
                evaluation_start=fold.get("evaluation_start") or fold.get("validation_start"),
                evaluation_end=fold.get("evaluation_end") or fold.get("validation_end"),
            )
            rows.append(
                {
                    "fold_id": fold_id,
                    "score": self._metric_score(result["metrics"]),
                    "output_dir": str(self._relative(result["output_dir"])),
                    **self._flatten_metrics(result["metrics"], prefix="metrics"),
                }
            )
        return rows

    def _evaluate_walk_forward_best(
        self,
        *,
        policy_config_path: Path,
        split_manifest: dict[str, Any],
        output_dir: Path,
        search_id: str,
        request: BudgetParameterSearchRequest,
        run_state_path: Path,
    ) -> dict[str, Any]:
        rows = []
        for fold in split_manifest.get("folds", []):
            fold_id = fold["fold_id"]
            result = self._run_execution_and_backtest(
                policy_config_path=policy_config_path,
                run_state_path=run_state_path,
                panel_path=self._resolve_path(fold.get("context_panel_path") or fold["validation_panel_path"]),
                returns_path=self._resolve_path(fold.get("context_returns_path") or fold["validation_returns_path"]),
                output_dir=output_dir / fold_id,
                policy_id=f"{search_id}_best_{fold_id}",
                backtest_id=f"{search_id}_best_{fold_id}",
                request=request,
                update_run_state=False,
                evaluation_start=fold.get("evaluation_start") or fold.get("validation_start"),
                evaluation_end=fold.get("evaluation_end") or fold.get("validation_end"),
            )
            rows.append(
                {
                    "fold_id": fold_id,
                    "score": self._metric_score(result["metrics"]),
                    "output_dir": str(self._relative(result["output_dir"])),
                    **self._flatten_metrics(result["metrics"], prefix="metrics"),
                }
            )
        scores = [float(row["score"]) for row in rows]
        return {
            "fold_count": len(rows),
            "mean_score": self._mean(scores),
            "std_score": self._std(scores),
            "min_score": min(scores) if scores else None,
            "max_score": max(scores) if scores else None,
            "folds": rows,
        }

    def _run_execution_and_backtest(
        self,
        *,
        policy_config_path: Path,
        run_state_path: Path,
        panel_path: Path,
        returns_path: Path,
        output_dir: Path,
        policy_id: str,
        backtest_id: str,
        request: BudgetParameterSearchRequest,
        update_run_state: bool,
        generate_chart: bool | None = None,
        evaluation_start: str | None = None,
        evaluation_end: str | None = None,
    ) -> dict[str, Any]:
        execution_dir = output_dir / "execution"
        backtest_dir = output_dir / "backtest"
        engine_result = BudgetPolicyEngine(config=self.config).run(
            BudgetPolicyEngineRequest(
                budget_run_state_path=run_state_path,
                policy_config_path=policy_config_path,
                output_dir=execution_dir,
                panel_ohlcv_path=panel_path,
                returns_wide_path=returns_path,
                policy_id=policy_id,
                update_run_state=False,
            )
        )
        weights_path = engine_result.daily_budget_weights_path
        sliced_weights_path = weights_path
        if evaluation_start or evaluation_end:
            sliced_weights_path = self._slice_weights_for_evaluation(
                weights_path=weights_path,
                output_path=execution_dir / "daily_budget_weights_evaluation.parquet",
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
            )
        backtest_result = BudgetBacktestService(config=self.config).run(
            BudgetBacktestRequest(
                budget_run_state_path=run_state_path,
                weights_path=sliced_weights_path,
                returns_wide_path=returns_path,
                output_dir=backtest_dir,
                backtest_id=backtest_id,
                benchmark=request.benchmark,
                update_run_state=update_run_state,
                generate_chart=request.generate_chart if generate_chart is None else generate_chart,
            )
        )
        return {
            "output_dir": output_dir,
            "execution_manifest_path": engine_result.manifest_path,
            "backtest_manifest_path": backtest_result.manifest_path,
            "weights_path": weights_path,
            "metrics": backtest_result.metrics,
        }

    def _slice_weights_for_evaluation(self, *, weights_path: Path, output_path: Path, evaluation_start: str | None, evaluation_end: str | None) -> Path:
        weights = pd.read_parquet(weights_path)
        weights.index = pd.to_datetime(weights.index).normalize()
        if evaluation_start:
            weights = weights.loc[weights.index >= pd.to_datetime(evaluation_start).normalize()]
        if evaluation_end:
            weights = weights.loc[weights.index <= pd.to_datetime(evaluation_end).normalize()]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        weights.to_parquet(output_path)
        return output_path

    def _metric_score(self, metrics: dict[str, Any]) -> float:
        sharpe = self._number(metrics.get("sharpe"))
        total_return = self._number(metrics.get("total_return"))
        max_drawdown = abs(self._number(metrics.get("max_drawdown")))
        excess_return = self._number(metrics.get("excess_total_return"))
        turnover = self._number(metrics.get("average_turnover"))
        return 2.00 * sharpe + 0.80 * total_return + 0.50 * excess_return - 0.80 * max_drawdown - 0.10 * turnover

    def _normalize_param_space(self, raw: dict[str, Any]) -> dict[str, list[Any]]:
        params = raw.get("params") if isinstance(raw.get("params"), dict) else raw
        return {name: self._values_from_spec(spec) for name, spec in params.items()}

    def _values_from_spec(self, spec: Any) -> list[Any]:
        if not isinstance(spec, dict):
            return [spec]
        if "choices" in spec:
            values = list(spec["choices"])
        elif "values" in spec:
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
                    values = [round(float(low) + index * actual_step, 10) for index in range(count)]
                else:
                    points = 7
                    values = [round(float(low) + (float(high) - float(low)) * index / (points - 1), 10) for index in range(points)]
        return [value for value in values if value is not None]

    def _apply_params(self, base_config: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        config = json.loads(json.dumps(base_config, ensure_ascii=False))
        for path, value in params.items():
            self._set_path(config, path, value)
        return config

    def _set_path(self, target: dict[str, Any], path: str, value: Any) -> None:
        parts = self._parse_path(path)
        current: Any = target
        for part in parts[:-1]:
            current = current[part]
        current[parts[-1]] = value

    def _parse_path(self, path: str) -> list[Any]:
        parts: list[Any] = []
        for token in path.split("."):
            while "[" in token:
                before, rest = token.split("[", 1)
                if before:
                    parts.append(before)
                index, token = rest.split("]", 1)
                parts.append(int(index))
                if token.startswith("."):
                    token = token[1:]
            if token:
                parts.append(token)
        return parts

    def _random_candidate(self, values: dict[str, list[Any]], rng: random.Random) -> dict[str, Any]:
        return {name: rng.choice(choices) for name, choices in values.items()}

    def _candidate_cache_key(self, *, base_config: dict[str, Any], split_manifest: dict[str, Any], params: dict[str, Any]) -> str:
        payload = {
            "score_formula_version": "budget_metric_score_v1_20260510",
            "base_policy_name": base_config.get("policy_name"),
            "params": params,
            "source_returns": split_manifest.get("source_returns_wide_path"),
            "train_returns": split_manifest.get("train_returns_path"),
            "validation_returns": split_manifest.get("validation_returns_path"),
            "folds": split_manifest.get("folds", []),
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

    def _resolve_split_manifest(self, data_split_manifest_path: Path | None, *, state: dict[str, Any]) -> Path:
        if data_split_manifest_path is not None:
            return self._resolve_path(data_split_manifest_path)
        path = state.get("data_split", {}).get("split_manifest")
        if not path:
            path = state.get("artifacts", {}).get("datasets", {}).get("budget_splits", {}).get("manifest_path")
        if not path:
            raise ValueError("缺少预算层 split_manifest，请先运行 budget split-data，或显式传入 --data-split-manifest-path。")
        return self._resolve_path(path)

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any], search_id: str) -> Path:
        if output_dir is not None:
            return self._resolve_path(output_dir)
        policies_dir = state.get("directories", {}).get("policies")
        if policies_dir:
            return self._resolve_path(policies_dir) / "searches" / search_id
        return self.config.root_dir / "artifacts" / "budget_runs" / str(state.get("budget_run_id")) / "policies" / "searches" / search_id

    def _make_search_id(self, base_config: dict[str, Any]) -> str:
        raw = str(base_config.get("policy_name") or "budget_policy")
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw).strip("_") or "budget_policy"
        return f"{safe}_search_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def _update_run_state(self, *, state_path: Path, state: dict[str, Any], search_id: str, search_result: dict[str, Any], search_result_path: Path) -> None:
        now = datetime.now().isoformat()
        state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("searches", {})[search_id] = {
            "search_result_path": str(self._relative(search_result_path)),
            "best_policy_config_path": search_result["best_policy_config_path"],
            "best_execution_manifest_path": search_result["best_execution_manifest_path"],
            "best_backtest_manifest_path": search_result["best_backtest_manifest_path"],
            "population_summary_path": search_result["population_summary_path"],
            "best_score": search_result["best_score"],
            "best_params": search_result["best_params"],
            "summary": search_result["summary"],
        }
        state.setdefault("strategy_search", {})["current_iteration"] = int(state.get("strategy_search", {}).get("current_iteration") or 0) + 1
        state.setdefault("strategy_search", {}).setdefault("attempt_ids", []).append(search_id)
        current_best = state.setdefault("strategy_search", {}).get("best_score")
        if current_best is None or float(search_result["best_score"]) > self._number(current_best):
            state["strategy_search"]["best_score"] = float(search_result["best_score"])
            state["strategy_search"]["best_attempt_id"] = search_id
        state["strategy_search"]["status"] = "running"
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetParameterSearchService",
                "event": "budget_parameter_search_completed",
                "summary": f"预算层参数搜索完成：{search_id}，最佳得分 {search_result['best_score']:.6f}。",
                "search_id": search_id,
                "search_result_path": str(self._relative(search_result_path)),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _flatten_metrics(self, metrics: dict[str, Any], *, prefix: str) -> dict[str, Any]:
        keys = [
            "total_return",
            "annual_return",
            "sharpe",
            "max_drawdown",
            "excess_total_return",
            "benchmark_total_return",
            "average_turnover",
            "total_transaction_cost",
            "average_gross_exposure",
            "average_holding_count",
        ]
        return {f"{prefix}_{key}": metrics.get(key) for key in keys}

    def _mean(self, values: list[float]) -> float:
        return float(sum(values) / len(values)) if values else 0.0

    def _std(self, values: list[float]) -> float:
        if len(values) <= 1:
            return 0.0
        mean = self._mean(values)
        return float(math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)))

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

    def _relative(self, path: str | Path) -> str:
        value = Path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)
