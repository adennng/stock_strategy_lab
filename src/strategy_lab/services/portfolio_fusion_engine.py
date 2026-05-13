from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field


class PortfolioFusionRequest(BaseModel):
    fusion_policy_path: Path
    fusion_policy_params: dict[str, Any] = Field(default_factory=dict)
    budget_weights: Any
    signal_targets: Any
    returns: Any
    signal_profile: dict[str, Any] | None = None
    market_context: dict[str, Any] | None = None

    class Config:
        arbitrary_types_allowed = True


class PortfolioFusionResult(BaseModel):
    final_weights: Any
    diagnostics: Any
    asset_diagnostics: Any
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True


class PortfolioFusionEngine:
    """组合层仓位融合执行器，只执行版本目录里的 fusion_policy.py。"""

    def run(self, request: PortfolioFusionRequest) -> PortfolioFusionResult:
        budget_weights = self._coerce_frame(request.budget_weights)
        signal_targets = self._coerce_frame(request.signal_targets)
        returns = self._coerce_frame(request.returns)
        budget_weights, signal_targets, returns = self._align_inputs(
            budget_weights=budget_weights,
            signal_targets=signal_targets,
            returns=returns,
        )
        return self._run_python_policy(
            policy_path=Path(request.fusion_policy_path),
            params=request.fusion_policy_params,
            budget_weights=budget_weights,
            signal_targets=signal_targets,
            returns=returns,
            signal_profile=request.signal_profile,
            market_context=request.market_context,
        )

    def _run_python_policy(
        self,
        *,
        policy_path: Path,
        params: dict[str, Any],
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        returns: pd.DataFrame,
        signal_profile: dict[str, Any] | None,
        market_context: dict[str, Any] | None,
    ) -> PortfolioFusionResult:
        module = self._load_module(policy_path)
        policy_cls = getattr(module, "PortfolioFusionPolicy", None)
        if policy_cls is not None:
            policy_obj = policy_cls(params)
            generate = getattr(policy_obj, "generate_weights", None)
        else:
            generate = getattr(module, "generate_weights", None)
        if generate is None:
            raise TypeError(f"{policy_path} 必须定义 PortfolioFusionPolicy.generate_weights 或模块级 generate_weights。")

        result = generate(
            budget_weights=budget_weights.copy(),
            signal_targets=signal_targets.copy(),
            returns=returns.copy(),
            signal_profile=signal_profile or {},
            market_context=market_context or {},
        )
        if isinstance(result, tuple):
            if len(result) == 2:
                weights, diagnostics = result
                asset_diagnostics = None
            elif len(result) == 3:
                weights, diagnostics, asset_diagnostics = result
            else:
                raise ValueError("fusion_policy.py 返回 tuple 时只能是 (weights, diagnostics) 或 (weights, diagnostics, asset_diagnostics)。")
        else:
            weights = result
            diagnostics = None
            asset_diagnostics = None

        final_weights = self._sanitize_weights(
            weights=weights,
            budget_weights=budget_weights,
            signal_targets=signal_targets,
            returns=returns,
        )
        diagnostics_frame = self._ensure_diagnostics(
            diagnostics=diagnostics,
            final_weights=final_weights,
            budget_weights=budget_weights,
            signal_targets=signal_targets,
        )
        asset_diagnostics_frame = self._ensure_asset_diagnostics(
            asset_diagnostics=asset_diagnostics,
            final_weights=final_weights,
            budget_weights=budget_weights,
            signal_targets=signal_targets,
        )
        policy_descriptor = {
            "policy_name": policy_path.stem,
            "fusion_type": "python_policy",
            "policy_path": str(policy_path),
        }
        return PortfolioFusionResult(
            final_weights=final_weights,
            diagnostics=diagnostics_frame,
            asset_diagnostics=asset_diagnostics_frame,
            summary=self._build_summary(diagnostics=diagnostics_frame, asset_diagnostics=asset_diagnostics_frame, policy=policy_descriptor),
            warnings=self._build_warnings(diagnostics=diagnostics_frame),
        )

    def _load_module(self, path: Path) -> ModuleType:
        if path.suffix.lower() != ".py":
            raise ValueError(f"组合层融合策略必须是 fusion_policy.py：{path}")
        if not path.exists():
            raise FileNotFoundError(f"fusion_policy.py 不存在：{path}")
        module_name = f"portfolio_fusion_policy_{path.stem}_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 fusion_policy.py：{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _sanitize_weights(
        self,
        *,
        weights: Any,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        returns: pd.DataFrame,
    ) -> pd.DataFrame:
        if not isinstance(weights, pd.DataFrame):
            raise TypeError("fusion_policy.py 的 weights 必须是 pandas DataFrame。")
        frame = weights.copy()
        frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
        frame.columns = [str(column).upper() for column in frame.columns]
        frame = frame.dropna(axis=0, how="all")
        index = budget_weights.index.intersection(signal_targets.index).intersection(returns.index).intersection(frame.index)
        columns = sorted(set(budget_weights.columns) & set(signal_targets.columns) & set(returns.columns) & set(frame.columns))
        if len(index) == 0 or not columns:
            raise ValueError("fusion_policy.py 输出的 weights 与预算、信号、收益率没有可对齐的日期或资产。")
        frame = frame.loc[index, columns].sort_index().fillna(0.0).clip(lower=0.0, upper=1.0)
        gross = frame.sum(axis=1)
        scale = (1.0 / gross.replace(0.0, np.nan)).clip(upper=1.0).fillna(1.0)
        frame = frame.mul(scale, axis=0).fillna(0.0)
        frame.index.name = "datetime"
        return frame

    def _ensure_diagnostics(
        self,
        *,
        diagnostics: Any,
        final_weights: pd.DataFrame,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
    ) -> pd.DataFrame:
        if isinstance(diagnostics, pd.DataFrame):
            frame = diagnostics.copy()
            if "datetime" in frame.columns:
                frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce").dt.normalize()
                frame = frame.dropna(subset=["datetime"]).set_index("datetime")
            else:
                frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
                frame = frame[~frame.index.isna()]
            frame = frame.reindex(final_weights.index)
        else:
            frame = pd.DataFrame(index=final_weights.index)

        previous = final_weights.shift(1).fillna(0.0)
        budget = budget_weights.reindex(index=final_weights.index, columns=final_weights.columns).fillna(0.0)
        signal = signal_targets.reindex(index=final_weights.index, columns=final_weights.columns).fillna(0.0)
        standard = {
            "target_gross": final_weights.sum(axis=1),
            "final_gross": final_weights.sum(axis=1),
            "cash_weight": (1.0 - final_weights.sum(axis=1)).clip(lower=0.0),
            "turnover": final_weights.subtract(previous, fill_value=0.0).abs().sum(axis=1),
            "budget_gross": budget.sum(axis=1),
            "signal_mean": signal.mean(axis=1),
            "signal_breadth": signal.ge(0.3).mean(axis=1),
            "over_budget_total": (final_weights - budget).clip(lower=0.0).sum(axis=1),
            "holding_count": final_weights.gt(1e-12).sum(axis=1),
        }
        for column, values in standard.items():
            if column not in frame.columns:
                frame[column] = values
        frame.index.name = "datetime"
        return frame.fillna(0.0).reset_index()

    def _ensure_asset_diagnostics(
        self,
        *,
        asset_diagnostics: Any,
        final_weights: pd.DataFrame,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
    ) -> pd.DataFrame:
        if isinstance(asset_diagnostics, pd.DataFrame) and {"datetime", "symbol", "final_weight"}.issubset(asset_diagnostics.columns):
            return asset_diagnostics.copy()
        budget = budget_weights.reindex(index=final_weights.index, columns=final_weights.columns).fillna(0.0)
        signal = signal_targets.reindex(index=final_weights.index, columns=final_weights.columns).fillna(0.0)
        rows: list[dict[str, Any]] = []
        for date in final_weights.index:
            over_budget = (final_weights.loc[date] - budget.loc[date]).clip(lower=0.0)
            for symbol in final_weights.columns:
                rows.append(
                    {
                        "datetime": date,
                        "symbol": symbol,
                        "budget_weight": float(budget.loc[date, symbol]),
                        "signal_target": float(signal.loc[date, symbol]),
                        "opportunity": float(signal.loc[date, symbol]),
                        "raw_weight": float(final_weights.loc[date, symbol]),
                        "final_weight": float(final_weights.loc[date, symbol]),
                        "over_budget": float(over_budget.loc[symbol]),
                    }
                )
        return pd.DataFrame(rows)

    def _align_inputs(
        self,
        *,
        budget_weights: pd.DataFrame,
        signal_targets: pd.DataFrame,
        returns: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        index = budget_weights.index.intersection(signal_targets.index).intersection(returns.index)
        columns = sorted(set(budget_weights.columns).intersection(signal_targets.columns).intersection(returns.columns))
        if len(index) == 0 or not columns:
            raise ValueError("budget_weights、signal_targets 和 returns 没有可对齐的日期或资产。")
        return (
            budget_weights.loc[index, columns].sort_index().fillna(0.0),
            signal_targets.loc[index, columns].sort_index().fillna(0.0),
            returns.loc[index, columns].sort_index().fillna(0.0),
        )

    @staticmethod
    def _coerce_frame(value: Any) -> pd.DataFrame:
        if not isinstance(value, pd.DataFrame):
            raise TypeError("PortfolioFusionEngine 输入必须是 pandas DataFrame。")
        frame = value.copy()
        frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
        frame = frame[~frame.index.isna()]
        frame.columns = [str(column).upper() for column in frame.columns]
        return frame.sort_index()

    @staticmethod
    def _build_summary(*, diagnostics: pd.DataFrame, asset_diagnostics: pd.DataFrame, policy: dict[str, Any]) -> dict[str, Any]:
        by_asset = asset_diagnostics.groupby("symbol", as_index=False).agg(
            budget_weight_mean=("budget_weight", "mean"),
            signal_target_mean=("signal_target", "mean"),
            final_weight_mean=("final_weight", "mean"),
            over_budget_mean=("over_budget", "mean"),
            opportunity_mean=("opportunity", "mean"),
        )
        return {
            "policy_name": policy.get("policy_name"),
            "fusion_type": policy.get("fusion_type"),
            "date_count": int(len(diagnostics)),
            "average_target_gross": float(diagnostics["target_gross"].mean()) if len(diagnostics) else 0.0,
            "average_final_gross": float(diagnostics["final_gross"].mean()) if len(diagnostics) else 0.0,
            "average_cash_weight": float(diagnostics["cash_weight"].mean()) if len(diagnostics) else 0.0,
            "average_turnover": float(diagnostics["turnover"].mean()) if len(diagnostics) else 0.0,
            "total_turnover": float(diagnostics["turnover"].sum()) if len(diagnostics) else 0.0,
            "average_over_budget_total": float(diagnostics["over_budget_total"].mean()) if len(diagnostics) else 0.0,
            "average_holding_count": float(diagnostics["holding_count"].mean()) if len(diagnostics) else 0.0,
            "top_final_weight_assets": by_asset.sort_values("final_weight_mean", ascending=False).head(10).to_dict(orient="records"),
            "top_over_budget_assets": by_asset.sort_values("over_budget_mean", ascending=False).head(10).to_dict(orient="records"),
        }

    @staticmethod
    def _build_warnings(*, diagnostics: pd.DataFrame) -> list[str]:
        warnings: list[str] = []
        if diagnostics.empty:
            return ["融合诊断为空。"]
        avg_final = float(diagnostics["final_gross"].mean())
        if avg_final < 0.2:
            warnings.append("平均最终敞口较低，可能存在资金利用不足。")
        if float(diagnostics["turnover"].mean()) > 0.5:
            warnings.append("平均日换手率较高，需要检查调仓约束。")
        if float(diagnostics["over_budget_total"].mean()) > 0.2:
            warnings.append("平均突破预算比例较高，需要检查预算约束或超预算逻辑。")
        return warnings
