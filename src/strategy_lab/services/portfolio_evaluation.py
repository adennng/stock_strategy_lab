from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_backtest import BudgetBacktestRequest, BudgetBacktestService
from strategy_lab.services.budget_policy_engine import BudgetPolicyEngine, BudgetPolicyEngineRequest
from strategy_lab.services.data_format import load_wide_parquet, normalize_symbol_series
from strategy_lab.services.portfolio_fusion_engine import PortfolioFusionEngine, PortfolioFusionRequest, PortfolioFusionResult
from strategy_lab.services.portfolio_run import PortfolioRunManager


class PortfolioEvaluationRequest(BaseModel):
    portfolio_run_state_path: Path
    version_id: str
    split_manifest_path: Path | None = None
    output_dir: Path | None = None
    benchmark: str | None = None
    initial_cash: float | None = None
    commission: float | None = None
    slippage_perc: float | None = None
    generate_chart: bool = True
    update_run_state: bool = True


class PortfolioEvaluationResult(BaseModel):
    portfolio_run_state_path: Path
    version_id: str
    output_dir: Path
    budget_execution_dir: Path
    backtest_dir: Path
    daily_budget_weights_path: Path
    daily_signal_targets_path: Path
    daily_final_weights_path: Path
    fusion_diagnostics_path: Path
    fusion_asset_diagnostics_path: Path
    fusion_diagnostics_json_path: Path
    fusion_diagnostics_report_path: Path
    backtest_manifest_path: Path
    evaluation_manifest_path: Path
    report_path: Path
    metrics_path: Path
    comparison_chart_path: Path | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class PortfolioEvaluationService:
    """组合层融合评估服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = PortfolioRunManager(config=self.config)

    def run(self, request: PortfolioEvaluationRequest) -> PortfolioEvaluationResult:
        state_path = self._resolve_path(request.portfolio_run_state_path)
        state = self.run_manager.load_state(state_path)
        version = self._get_or_register_version(state_path=state_path, state=state, version_id=request.version_id)
        fusion_manifest_path = self._resolve_path(version["fusion_manifest_path"])
        fusion_manifest = self._read_json(fusion_manifest_path)
        fusion_policy_path = self._resolve_fusion_policy_path(fusion_manifest=fusion_manifest, version=version)
        fusion_policy = self._build_fusion_policy_descriptor(fusion_policy_path=fusion_policy_path, fusion_manifest=fusion_manifest, version=version)
        fusion_policy_params = self._resolve_fusion_policy_params(fusion_manifest=fusion_manifest, version=version)
        signal_profile = self._load_optional_json(self._resolve_optional_profile_path(state, "portfolio_signal_profile", "signal_profiles_path"))
        market_context = self._load_optional_json(self._resolve_optional_profile_path(state, "portfolio_profile", "portfolio_profile_path"))
        reference_symbols = self._reference_symbols_from_fusion_manifest(fusion_manifest)
        split_manifest_path = self._resolve_split_manifest_path(request.split_manifest_path, state=state)
        split_manifest = self._read_json(split_manifest_path)
        panel_path, returns_path = self._resolve_data_paths(split_manifest)
        output_dir = self._resolve_output_dir(request.output_dir, version=version)
        output_dir.mkdir(parents=True, exist_ok=True)

        budget_state_path = self._resolve_path(state["source_artifacts"]["budget"]["budget_run_state_path"])
        budget_policy_config_path = self._resolve_path(fusion_manifest["budget"]["budget_policy_config_path"])
        budget_execution_dir = output_dir / "budget_execution"
        budget_result = BudgetPolicyEngine(config=self.config).run(
            BudgetPolicyEngineRequest(
                budget_run_state_path=budget_state_path,
                policy_config_path=budget_policy_config_path,
                output_dir=budget_execution_dir,
                panel_ohlcv_path=panel_path,
                returns_wide_path=returns_path,
                policy_id=f"{request.version_id}_budget",
                update_run_state=False,
            )
        )

        budget_weights = self._load_wide_frame(budget_result.daily_budget_weights_path, reference_symbols=reference_symbols)
        returns = self._load_wide_frame(returns_path, reference_symbols=reference_symbols)
        panel = self._load_panel(panel_path, reference_symbols=reference_symbols)
        signal_targets, signal_warnings = self._build_signal_targets(
            fusion_manifest=fusion_manifest,
            panel=panel,
            target_index=budget_weights.index.intersection(returns.index),
            target_columns=list(budget_weights.columns),
        )
        fusion_result = PortfolioFusionEngine().run(
            PortfolioFusionRequest(
                fusion_policy_path=fusion_policy_path,
                fusion_policy_params=fusion_policy_params,
                budget_weights=budget_weights,
                signal_targets=signal_targets,
                returns=returns,
                signal_profile=signal_profile,
                market_context=market_context,
            )
        )
        final_weights = fusion_result.final_weights

        aligned_budget_weights = budget_weights.reindex(index=final_weights.index, columns=final_weights.columns).fillna(0.0)
        aligned_signal_targets = signal_targets.reindex(index=final_weights.index, columns=final_weights.columns).fillna(0.0)

        daily_budget_weights_path = output_dir / "daily_budget_weights.parquet"
        daily_signal_targets_path = output_dir / "daily_signal_targets.parquet"
        daily_final_weights_path = output_dir / "daily_final_weights.parquet"
        fusion_diagnostics_path = output_dir / "fusion_diagnostics.parquet"
        fusion_asset_diagnostics_path = output_dir / "fusion_asset_diagnostics.parquet"
        fusion_diagnostics_json_path = output_dir / "fusion_diagnostics.json"
        fusion_diagnostics_report_path = output_dir / "fusion_diagnostics.md"
        aligned_budget_weights.to_parquet(daily_budget_weights_path)
        aligned_signal_targets.to_parquet(daily_signal_targets_path)
        final_weights.to_parquet(daily_final_weights_path)
        fusion_result.diagnostics.to_parquet(fusion_diagnostics_path)
        fusion_result.asset_diagnostics.to_parquet(fusion_asset_diagnostics_path)
        fusion_diagnostics_payload = {
            "summary": fusion_result.summary,
            "warnings": fusion_result.warnings,
            "diagnostics_path": self._relative(fusion_diagnostics_path),
            "asset_diagnostics_path": self._relative(fusion_asset_diagnostics_path),
        }
        fusion_diagnostics_json_path.write_text(
            json.dumps(fusion_diagnostics_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        fusion_diagnostics_report_path.write_text(
            self._format_fusion_diagnostics_report(fusion_result),
            encoding="utf-8",
        )

        final_returns = returns.reindex(index=final_weights.index, columns=final_weights.columns).fillna(0.0)
        returns_path_for_backtest = output_dir / "aligned_returns_wide.parquet"
        final_returns.to_parquet(returns_path_for_backtest)

        backtest_dir = output_dir / "backtest"
        backtest_result = BudgetBacktestService(config=self.config).run(
            BudgetBacktestRequest(
                budget_run_state_path=budget_state_path,
                weights_path=daily_final_weights_path,
                returns_wide_path=returns_path_for_backtest,
                output_dir=backtest_dir,
                backtest_id=f"{request.version_id}_fusion",
                benchmark=request.benchmark,
                initial_cash=request.initial_cash,
                commission=request.commission,
                slippage_perc=request.slippage_perc,
                update_run_state=False,
                generate_chart=request.generate_chart,
            )
        )

        report_path = output_dir / "evaluation_summary.md"
        manifest_path = output_dir / "evaluation_manifest.json"
        warnings = signal_warnings + list(fusion_result.warnings) + list(backtest_result.warnings)
        manifest = self._build_manifest(
            request=request,
            state=state,
            fusion_manifest_path=fusion_manifest_path,
            fusion_policy_path=fusion_policy_path,
            fusion_policy=fusion_policy,
            split_manifest_path=split_manifest_path,
            panel_path=panel_path,
            returns_path=returns_path_for_backtest,
            budget_result=budget_result,
            backtest_result=backtest_result,
            output_dir=output_dir,
            daily_budget_weights_path=daily_budget_weights_path,
            daily_signal_targets_path=daily_signal_targets_path,
            daily_final_weights_path=daily_final_weights_path,
            fusion_result=fusion_result,
            fusion_diagnostics_path=fusion_diagnostics_path,
            fusion_asset_diagnostics_path=fusion_asset_diagnostics_path,
            fusion_diagnostics_json_path=fusion_diagnostics_json_path,
            fusion_diagnostics_report_path=fusion_diagnostics_report_path,
            warnings=warnings,
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report_path.write_text(self._format_report(manifest), encoding="utf-8")

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                version_id=request.version_id,
                manifest=manifest,
                manifest_path=manifest_path,
                report_path=report_path,
                metrics=backtest_result.metrics,
            )

        return PortfolioEvaluationResult(
            portfolio_run_state_path=state_path,
            version_id=request.version_id,
            output_dir=output_dir,
            budget_execution_dir=budget_execution_dir,
            backtest_dir=backtest_dir,
            daily_budget_weights_path=daily_budget_weights_path,
            daily_signal_targets_path=daily_signal_targets_path,
            daily_final_weights_path=daily_final_weights_path,
            fusion_diagnostics_path=fusion_diagnostics_path,
            fusion_asset_diagnostics_path=fusion_asset_diagnostics_path,
            fusion_diagnostics_json_path=fusion_diagnostics_json_path,
            fusion_diagnostics_report_path=fusion_diagnostics_report_path,
            backtest_manifest_path=backtest_result.manifest_path,
            evaluation_manifest_path=manifest_path,
            report_path=report_path,
            metrics_path=backtest_result.metrics_path,
            comparison_chart_path=backtest_result.comparison_chart_path,
            metrics=backtest_result.metrics,
            warnings=warnings,
        )

    def _build_signal_targets(
        self,
        *,
        fusion_manifest: dict[str, Any],
        panel: pd.DataFrame,
        target_index: pd.DatetimeIndex,
        target_columns: list[str],
    ) -> tuple[pd.DataFrame, list[str]]:
        warnings: list[str] = []
        targets = pd.DataFrame(0.0, index=target_index, columns=target_columns, dtype=float)
        panel_by_symbol = {symbol: frame.sort_values("datetime").reset_index(drop=True) for symbol, frame in panel.groupby("symbol")}
        for item in fusion_manifest.get("signals", []):
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in targets.columns:
                warnings.append(f"{symbol} 不在预算权重列中，跳过信号计算。")
                continue
            symbol_panel = panel_by_symbol.get(symbol)
            if symbol_panel is None or symbol_panel.empty:
                warnings.append(f"{symbol} 没有可用行情，信号目标仓位置为 0。")
                continue
            strategy_path = self._resolve_path(item["strategy_path"])
            strategy_meta_path = self._resolve_path(item.get("strategy_meta_path") or strategy_path.parent / "strategy_meta.json")
            param_space_path = self._resolve_path(item.get("param_space_path") or strategy_path.parent / "param_space.json")
            strategy_params_path = self._resolve_path(item.get("strategy_params_path") or strategy_path.parent / "strategy_params.json")
            class_name = self._resolve_strategy_class_name(strategy_meta_path)
            strategy_cls = self._load_strategy_class(strategy_path, class_name)
            params = self._resolve_signal_params(strategy_params_path=strategy_params_path, param_space_path=param_space_path)
            strategy = strategy_cls(params)
            symbol_targets = self._run_signal_strategy(strategy=strategy, symbol_panel=symbol_panel)
            targets[symbol] = symbol_targets.reindex(targets.index).ffill().fillna(0.0).clip(lower=0.0, upper=1.0)
        return targets.fillna(0.0), warnings

    def _run_signal_strategy(self, *, strategy: Any, symbol_panel: pd.DataFrame) -> pd.Series:
        values: list[dict[str, Any]] = []
        previous = 0.0
        for idx, row in symbol_panel.iterrows():
            history = symbol_panel.iloc[: idx + 1].copy()
            try:
                target = float(strategy.suggest(history, current_position_in_budget=previous))
            except TypeError:
                target = float(strategy.suggest(history))
            target = max(0.0, min(1.0, target))
            values.append({"datetime": row["datetime"], "target": target})
            previous = target
        series = pd.DataFrame(values).drop_duplicates("datetime", keep="last").set_index("datetime")["target"]
        series.index = pd.to_datetime(series.index).normalize()
        return series.sort_index()

    def _build_manifest(
        self,
        *,
        request: PortfolioEvaluationRequest,
        state: dict[str, Any],
        fusion_manifest_path: Path,
        fusion_policy_path: Path,
        fusion_policy: dict[str, Any],
        split_manifest_path: Path,
        panel_path: Path,
        returns_path: Path,
        budget_result: Any,
        backtest_result: Any,
        output_dir: Path,
        daily_budget_weights_path: Path,
        daily_signal_targets_path: Path,
        daily_final_weights_path: Path,
        fusion_result: PortfolioFusionResult,
        fusion_diagnostics_path: Path,
        fusion_asset_diagnostics_path: Path,
        fusion_diagnostics_json_path: Path,
        fusion_diagnostics_report_path: Path,
        warnings: list[str],
    ) -> dict[str, Any]:
        return {
            "created_at": datetime.now().isoformat(),
            "portfolio_run_id": state.get("portfolio_run_id"),
            "version_id": request.version_id,
            "fusion_type": fusion_policy.get("fusion_type"),
            "fusion_manifest_path": self._relative(fusion_manifest_path),
            "fusion_policy_path": self._relative(fusion_policy_path),
            "split_manifest_path": self._relative(split_manifest_path),
            "panel_ohlcv_path": self._relative(panel_path),
            "returns_wide_path": self._relative(returns_path),
            "output_dir": self._relative(output_dir),
            "budget_execution": {
                "output_dir": self._relative(budget_result.output_dir),
                "manifest_path": self._relative(budget_result.manifest_path),
                "daily_budget_weights_path": self._relative(budget_result.daily_budget_weights_path),
                "daily_scores_path": self._relative(budget_result.daily_scores_path),
                "turnover_path": self._relative(budget_result.turnover_path),
                "summary": budget_result.summary,
            },
            "fusion_outputs": {
                "daily_budget_weights_path": self._relative(daily_budget_weights_path),
                "daily_signal_targets_path": self._relative(daily_signal_targets_path),
                "daily_final_weights_path": self._relative(daily_final_weights_path),
                "fusion_diagnostics_path": self._relative(fusion_diagnostics_path),
                "fusion_asset_diagnostics_path": self._relative(fusion_asset_diagnostics_path),
                "fusion_diagnostics_json_path": self._relative(fusion_diagnostics_json_path),
                "fusion_diagnostics_report_path": self._relative(fusion_diagnostics_report_path),
                "summary": fusion_result.summary,
            },
            "backtest": {
                "output_dir": self._relative(backtest_result.output_dir),
                "manifest_path": self._relative(backtest_result.manifest_path),
                "equity_curve_path": self._relative(backtest_result.equity_curve_path),
                "benchmark_curve_path": self._relative(backtest_result.benchmark_curve_path),
                "orders_path": self._relative(backtest_result.orders_path),
                "holdings_path": self._relative(backtest_result.holdings_path),
                "metrics_path": self._relative(backtest_result.metrics_path),
                "report_path": self._relative(backtest_result.report_path),
                "comparison_chart_path": self._relative(backtest_result.comparison_chart_path) if backtest_result.comparison_chart_path else None,
                "metrics": backtest_result.metrics,
            },
            "warnings": warnings,
        }

    def _format_report(self, manifest: dict[str, Any]) -> str:
        metrics = manifest.get("backtest", {}).get("metrics", {})
        fusion_summary = manifest.get("fusion_outputs", {}).get("summary", {})
        keys = [
            "total_return",
            "annual_return",
            "annual_volatility",
            "sharpe",
            "max_drawdown",
            "benchmark_name",
            "benchmark_total_return",
            "benchmark_sharpe",
            "benchmark_max_drawdown",
            "excess_total_return",
            "average_turnover",
            "average_gross_exposure",
            "average_holding_count",
        ]
        lines = ["# Portfolio Fusion Evaluation", ""]
        lines.append(f"- portfolio_run_id: {manifest.get('portfolio_run_id')}")
        lines.append(f"- version_id: {manifest.get('version_id')}")
        lines.append("")
        lines.append("## Metrics")
        for key in keys:
            lines.append(f"- {key}: {metrics.get(key)}")
        lines.append("")
        lines.append("## Fusion Diagnostics")
        for key in [
            "average_target_gross",
            "average_final_gross",
            "average_cash_weight",
            "average_turnover",
            "average_over_budget_total",
            "average_holding_count",
        ]:
            lines.append(f"- {key}: {fusion_summary.get(key)}")
        if manifest.get("warnings"):
            lines.append("")
            lines.append("## Warnings")
            for warning in manifest["warnings"]:
                lines.append(f"- {warning}")
        lines.append("")
        return "\n".join(lines)

    def _format_fusion_diagnostics_report(self, fusion_result: PortfolioFusionResult) -> str:
        lines = ["# Portfolio Fusion Diagnostics", ""]
        lines.append("## Summary")
        for key, value in fusion_result.summary.items():
            lines.append(f"- {key}: {value}")
        if fusion_result.warnings:
            lines.append("")
            lines.append("## Warnings")
            for warning in fusion_result.warnings:
                lines.append(f"- {warning}")
        lines.append("")
        lines.append("## Files")
        lines.append("- fusion_diagnostics.parquet: 每日组合层融合诊断。")
        lines.append("- fusion_asset_diagnostics.parquet: 每日每个资产的预算、信号、机会分和最终权重。")
        lines.append("")
        return "\n".join(lines)

    def _update_run_state(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        version_id: str,
        manifest: dict[str, Any],
        manifest_path: Path,
        report_path: Path,
        metrics: dict[str, Any],
    ) -> None:
        now = datetime.now().isoformat()
        for version in state.get("versions", []):
            if version.get("version_id") == version_id:
                version["status"] = "evaluated"
                version["evaluation"] = {
                    "manifest_path": self._relative(manifest_path),
                    "report_path": self._relative(report_path),
                    "daily_final_weights_path": manifest["fusion_outputs"]["daily_final_weights_path"],
                    "fusion_diagnostics_path": manifest["fusion_outputs"].get("fusion_diagnostics_path"),
                    "fusion_asset_diagnostics_path": manifest["fusion_outputs"].get("fusion_asset_diagnostics_path"),
                    "fusion_diagnostics_report_path": manifest["fusion_outputs"].get("fusion_diagnostics_report_path"),
                    "metrics_path": manifest["backtest"]["metrics_path"],
                    "comparison_chart_path": manifest["backtest"].get("comparison_chart_path"),
                    "metrics": metrics,
                    "fusion_summary": manifest["fusion_outputs"].get("summary", {}),
                    "updated_at": now,
                }
                break
        state.setdefault("artifacts", {}).setdefault("evaluations", {})[version_id] = {
            "manifest_path": self._relative(manifest_path),
            "report_path": self._relative(report_path),
            "daily_final_weights_path": manifest["fusion_outputs"]["daily_final_weights_path"],
            "fusion_diagnostics_path": manifest["fusion_outputs"].get("fusion_diagnostics_path"),
            "fusion_asset_diagnostics_path": manifest["fusion_outputs"].get("fusion_asset_diagnostics_path"),
            "fusion_diagnostics_report_path": manifest["fusion_outputs"].get("fusion_diagnostics_report_path"),
            "metrics_path": manifest["backtest"]["metrics_path"],
            "comparison_chart_path": manifest["backtest"].get("comparison_chart_path"),
            "metrics": metrics,
            "fusion_summary": manifest["fusion_outputs"].get("summary", {}),
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "PortfolioEvaluationService",
                "event": "portfolio_evaluation_completed",
                "summary": f"组合层版本 {version_id} 的评估已完成。",
                "version_id": version_id,
                "manifest_path": self._relative(manifest_path),
            }
        )
        state["status"] = "evaluated"
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _resolve_signal_params(self, *, strategy_params_path: Path, param_space_path: Path) -> dict[str, Any]:
        if strategy_params_path.exists():
            payload = self._read_json(strategy_params_path)
            if isinstance(payload.get("params"), dict):
                return dict(payload["params"])
            return dict(payload)

        params: dict[str, Any] = {}
        if param_space_path.exists():
            param_space = self._read_json(param_space_path)
            for key, spec in param_space.items():
                if isinstance(spec, dict) and "default" in spec:
                    params[key] = spec["default"]
        return params

    def _resolve_strategy_class_name(self, strategy_meta_path: Path) -> str:
        if strategy_meta_path.exists():
            meta = self._read_json(strategy_meta_path)
            return str(meta.get("strategy_class_name") or "Strategy")
        return "Strategy"

    def _load_strategy_class(self, strategy_path: Path, class_name: str) -> type:
        module = self._load_module(strategy_path)
        strategy_cls = getattr(module, class_name, None)
        if strategy_cls is None:
            raise AttributeError(f"{strategy_path} 中没有策略类 {class_name}。")
        if not hasattr(strategy_cls, "suggest"):
            raise TypeError(f"{strategy_path}:{class_name} 缺少 suggest 方法。")
        return strategy_cls

    def _load_module(self, path: Path) -> ModuleType:
        module_name = f"portfolio_signal_{path.stem}_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载策略脚本：{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _build_fusion_policy_descriptor(
        self,
        *,
        fusion_policy_path: Path,
        fusion_manifest: dict[str, Any],
        version: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "policy_name": fusion_manifest.get("policy_name") or version.get("version_id") or fusion_policy_path.stem,
            "fusion_type": "python_policy",
            "fusion_policy_path": self._relative(fusion_policy_path),
        }

    def _resolve_fusion_policy_params(self, *, fusion_manifest: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
        raw = fusion_manifest.get("param_space_path") or version.get("param_space_path")
        if not raw:
            version_dir = self._resolve_path(version.get("version_dir") or "")
            raw = version_dir / "param_space.json"
        path = self._resolve_path(raw)
        if not path.exists():
            return {}
        payload = self._read_json(path)
        if isinstance(payload.get("params"), dict):
            return dict(payload["params"])
        params: dict[str, Any] = {}
        for key, spec in payload.items():
            if isinstance(spec, dict) and "default" in spec:
                params[key] = spec["default"]
        return params

    def _resolve_optional_profile_path(self, state: dict[str, Any], profile_key: str, path_key: str) -> Path | None:
        raw = state.get("artifacts", {}).get("profiles", {}).get(profile_key, {}).get(path_key)
        if not raw and profile_key == "portfolio_profile":
            raw = state.get("profile", {}).get(path_key)
        if not raw and profile_key == "portfolio_signal_profile":
            raw = state.get("signal_profile", {}).get(path_key)
        if not raw:
            return None
        path = self._resolve_path(raw)
        return path if path.exists() else None

    def _load_optional_json(self, path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        try:
            return self._read_json(path)
        except Exception:
            return None

    def _resolve_fusion_policy_path(self, *, fusion_manifest: dict[str, Any], version: dict[str, Any]) -> Path:
        raw = fusion_manifest.get("fusion_policy_path") or version.get("fusion_policy_path")
        if not raw:
            version_dir = self._resolve_path(version.get("version_dir") or "")
            raw = version_dir / "fusion_policy.py"
        path = self._resolve_path(raw)
        if path.suffix.lower() != ".py":
            raise ValueError(f"组合层版本必须使用 fusion_policy.py：{path}")
        if not path.exists():
            raise FileNotFoundError(f"组合层版本缺少 fusion_policy.py：{path}")
        return path

    def _get_version(self, state: dict[str, Any], version_id: str) -> dict[str, Any]:
        for version in state.get("versions", []):
            if version.get("version_id") == version_id:
                return version
        raise ValueError(f"portfolio_run_state.json 中不存在 version_id={version_id}。")

    def _get_or_register_version(self, *, state_path: Path, state: dict[str, Any], version_id: str) -> dict[str, Any]:
        for version in state.get("versions", []):
            if version.get("version_id") == version_id:
                return version
        return self._register_existing_version_dir(state_path=state_path, state=state, version_id=version_id)

    def _register_existing_version_dir(self, *, state_path: Path, state: dict[str, Any], version_id: str) -> dict[str, Any]:
        versions_dir = self._resolve_path(state.get("directories", {}).get("versions") or state_path.parent / "versions")
        version_dir = versions_dir / self._safe_name(version_id)
        if not version_dir.exists():
            raise ValueError(f"portfolio_run_state.json 中不存在 version_id={version_id}，且版本目录不存在：{version_dir}")

        required_files = {
            "fusion_manifest_path": version_dir / "fusion_manifest.json",
            "fusion_policy_path": version_dir / "fusion_policy.py",
            "param_space_path": version_dir / "param_space.json",
            "fusion_policy_spec_path": version_dir / "fusion_policy_spec.md",
            "fusion_policy_meta_path": version_dir / "fusion_policy_meta.json",
        }
        missing = [name for name, path in required_files.items() if not path.exists()]
        if missing:
            details = {name: str(required_files[name]) for name in missing}
            raise FileNotFoundError(f"未登记版本 {version_id} 的文件不完整，缺少：{details}")

        fusion_manifest = self._read_json(required_files["fusion_manifest_path"])
        self._validate_fusion_manifest_for_registration(fusion_manifest=fusion_manifest, version_id=version_id)
        now = datetime.now().isoformat()
        version_payload = {
            "version_id": version_id,
            "status": "created",
            "version_role": fusion_manifest.get("version_role") or "candidate",
            "source_version_id": fusion_manifest.get("source_version_id"),
            "version_dir": self._relative(version_dir),
            "fusion_manifest_path": self._relative(required_files["fusion_manifest_path"]),
            "fusion_policy_path": self._relative(required_files["fusion_policy_path"]),
            "param_space_path": self._relative(required_files["param_space_path"]),
            "fusion_policy_spec_path": self._relative(required_files["fusion_policy_spec_path"]),
            "fusion_policy_meta_path": self._relative(required_files["fusion_policy_meta_path"]),
            "created_at": fusion_manifest.get("created_at") or now,
            "registered_at": now,
            "summary": fusion_manifest.get("notes") or fusion_manifest.get("change_summary") or "自动登记的组合层版本。",
        }
        state.setdefault("versions", []).append(version_payload)
        state["current_version"] = version_id
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "PortfolioEvaluationService",
                "event": "portfolio_version_auto_registered",
                "summary": f"评估前自动登记组合层版本 {version_id}。",
                "version_id": version_id,
                "fusion_manifest_path": version_payload["fusion_manifest_path"],
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)
        return version_payload

    def _validate_fusion_manifest_for_registration(self, *, fusion_manifest: dict[str, Any], version_id: str) -> None:
        manifest_version_id = fusion_manifest.get("version_id")
        if manifest_version_id and str(manifest_version_id) != version_id:
            raise ValueError(f"fusion_manifest.json 的 version_id={manifest_version_id}，与命令参数 version_id={version_id} 不一致。")
        if not isinstance(fusion_manifest.get("budget"), dict) or not fusion_manifest["budget"].get("budget_policy_config_path"):
            raise ValueError("fusion_manifest.json 缺少 budget.budget_policy_config_path。")
        signals = fusion_manifest.get("signals")
        if not isinstance(signals, list) or not signals:
            raise ValueError("fusion_manifest.json 缺少 signals 列表。")
        for item in signals:
            symbol = item.get("symbol")
            if not symbol:
                raise ValueError("fusion_manifest.json 的 signals 中存在缺少 symbol 的条目。")
            for key in ["strategy_path", "param_space_path", "strategy_meta_path"]:
                raw = item.get(key)
                if not raw:
                    raise ValueError(f"fusion_manifest.json 的 {symbol} 缺少 {key}。")
                path = self._resolve_path(raw)
                if not path.exists():
                    raise FileNotFoundError(f"fusion_manifest.json 的 {symbol}.{key} 指向不存在的文件：{path}")

    @staticmethod
    def _safe_name(value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value)).strip("_") or "version"

    def _resolve_split_manifest_path(self, split_manifest_path: Path | None, *, state: dict[str, Any]) -> Path:
        if split_manifest_path:
            return self._resolve_path(split_manifest_path)
        raw = state.get("data", {}).get("split_manifest") or state.get("artifacts", {}).get("datasets", {}).get("portfolio_splits", {}).get("manifest_path")
        if not raw:
            raise ValueError("未传 split_manifest_path，且 portfolio_run_state.json 中没有 data.split_manifest。")
        return self._resolve_path(raw)

    def _resolve_data_paths(self, split_manifest: dict[str, Any]) -> tuple[Path, Path]:
        panel_raw = split_manifest.get("full_panel_path") or split_manifest.get("source_panel_ohlcv_path")
        returns_raw = split_manifest.get("full_returns_path") or split_manifest.get("source_returns_wide_path")
        if not panel_raw or not returns_raw:
            raise ValueError("split_manifest.json 缺少 full/source panel 或 returns 路径。")
        return self._resolve_path(panel_raw), self._resolve_path(returns_raw)

    def _resolve_output_dir(self, output_dir: Path | None, *, version: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        version_dir = self._resolve_path(version["version_dir"])
        return version_dir / "evaluation"

    def _load_panel(self, panel_path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        if not panel_path.exists():
            raise FileNotFoundError(f"panel_ohlcv 文件不存在：{panel_path}")
        panel = pd.read_parquet(panel_path)
        required = {"datetime", "symbol", "close"}
        missing = required - set(panel.columns)
        if missing:
            raise ValueError(f"panel_ohlcv 缺少字段：{sorted(missing)}")
        panel = panel.copy()
        panel["datetime"] = pd.to_datetime(panel["datetime"], errors="coerce").dt.normalize()
        panel["symbol"] = normalize_symbol_series(panel["symbol"], reference_symbols=reference_symbols)
        panel = panel.dropna(subset=["datetime", "symbol"]).sort_values(["symbol", "datetime"]).reset_index(drop=True)
        return panel

    def _load_wide_frame(self, path: Path, *, reference_symbols: list[str] | None = None) -> pd.DataFrame:
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        return load_wide_parquet(path, reference_symbols=reference_symbols)

    def _reference_symbols_from_fusion_manifest(self, fusion_manifest: dict[str, Any]) -> list[str]:
        symbols = [str(item.get("symbol") or "").upper() for item in fusion_manifest.get("signals", []) if item.get("symbol")]
        return sorted({item for item in symbols if item})

    def _read_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: str | Path | None) -> str | None:
        if path is None:
            return None
        value = self._resolve_path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)
