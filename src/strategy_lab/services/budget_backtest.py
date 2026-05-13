from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_run import BudgetRunManager
from strategy_lab.services.data_format import load_wide_parquet, normalize_symbol


class BudgetBacktestRequest(BaseModel):
    budget_run_state_path: Path
    weights_path: Path | None = None
    policy_execution_manifest_path: Path | None = None
    returns_wide_path: Path | None = None
    output_dir: Path | None = None
    backtest_id: str | None = None
    benchmark: str | None = None
    initial_cash: float | None = None
    commission: float | None = None
    slippage_perc: float | None = None
    update_run_state: bool = True
    generate_chart: bool = True


class BudgetBacktestResult(BaseModel):
    budget_run_state_path: Path
    output_dir: Path
    backtest_id: str
    weights_path: Path
    returns_wide_path: Path
    equity_curve_path: Path
    benchmark_curve_path: Path
    orders_path: Path
    holdings_path: Path
    metrics_path: Path
    report_path: Path
    manifest_path: Path
    comparison_chart_path: Path | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class BudgetBacktestService:
    """预算层组合回测服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetBacktestRequest) -> BudgetBacktestResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        manifest = self._load_policy_execution_manifest(request.policy_execution_manifest_path)
        weights_path = self._resolve_weights_path(request.weights_path, manifest)
        returns_path = self._resolve_returns_path(state, request.returns_wide_path)
        reference_symbols = self._reference_symbols_from_state(state)
        weights = self._load_weights(weights_path, reference_symbols=reference_symbols)
        returns = self._load_returns(returns_path, reference_symbols=reference_symbols)
        weights, returns = self._align_weights_and_returns(weights, returns)
        settings = self._resolve_settings(request, state)
        backtest_id = request.backtest_id or self._make_backtest_id(manifest, weights_path)
        output_dir = self._resolve_output_dir(request.output_dir, state=state, backtest_id=backtest_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        equity, orders, holdings = self._simulate(
            weights=weights,
            returns=returns,
            initial_cash=settings["initial_cash"],
            cost_rate=settings["commission"] + settings["slippage_perc"],
        )
        benchmark_name = request.benchmark or state.get("task", {}).get("benchmark") or "equal_weight_rebalance"
        benchmark = self._build_benchmark_curve(
            returns=returns,
            initial_cash=settings["initial_cash"],
            benchmark=benchmark_name,
            cost_rate=settings["commission"] + settings["slippage_perc"],
        )
        equity = self._attach_benchmark(equity, benchmark)
        metrics = self._compute_metrics(
            equity=equity,
            orders=orders,
            holdings=holdings,
            initial_cash=settings["initial_cash"],
            benchmark_name=benchmark_name,
        )
        metrics.update(
            {
                "backtest_id": backtest_id,
                "weights_path": str(self._relative(weights_path)),
                "returns_wide_path": str(self._relative(returns_path)),
                "budget_run_state_path": str(self._relative(state_path)),
                "settings": settings,
            }
        )

        equity_path = output_dir / "equity_curve.parquet"
        benchmark_path = output_dir / "benchmark_curve.parquet"
        orders_path = output_dir / "orders.parquet"
        holdings_path = output_dir / "holdings.parquet"
        metrics_path = output_dir / "metrics.json"
        report_path = output_dir / "report.md"
        manifest_path = output_dir / "budget_backtest_manifest.json"
        chart_path = output_dir / "budget_vs_benchmark.png"

        equity.to_parquet(equity_path, index=False)
        benchmark.to_parquet(benchmark_path, index=False)
        orders.to_parquet(orders_path, index=False)
        holdings.to_parquet(holdings_path, index=False)
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report_path.write_text(self._format_report(metrics), encoding="utf-8")
        actual_chart_path = None
        if request.generate_chart:
            self._write_comparison_chart(equity, chart_path)
            actual_chart_path = chart_path if chart_path.exists() else None

        manifest_payload = {
            "created_at": datetime.now().isoformat(),
            "budget_run_id": state.get("budget_run_id"),
            "backtest_id": backtest_id,
            "policy_execution_manifest_path": str(self._relative(manifest.get("_path"))) if manifest.get("_path") else None,
            "weights_path": str(self._relative(weights_path)),
            "returns_wide_path": str(self._relative(returns_path)),
            "output_dir": str(self._relative(output_dir)),
            "equity_curve_path": str(self._relative(equity_path)),
            "benchmark_curve_path": str(self._relative(benchmark_path)),
            "orders_path": str(self._relative(orders_path)),
            "holdings_path": str(self._relative(holdings_path)),
            "metrics_path": str(self._relative(metrics_path)),
            "report_path": str(self._relative(report_path)),
            "comparison_chart_path": str(self._relative(actual_chart_path)) if actual_chart_path else None,
            "metrics": metrics,
            "warnings": [],
        }
        manifest_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                backtest_id=backtest_id,
                manifest=manifest_payload,
                manifest_path=manifest_path,
            )

        return BudgetBacktestResult(
            budget_run_state_path=state_path,
            output_dir=output_dir,
            backtest_id=backtest_id,
            weights_path=weights_path,
            returns_wide_path=returns_path,
            equity_curve_path=equity_path,
            benchmark_curve_path=benchmark_path,
            orders_path=orders_path,
            holdings_path=holdings_path,
            metrics_path=metrics_path,
            report_path=report_path,
            manifest_path=manifest_path,
            comparison_chart_path=actual_chart_path,
            metrics=metrics,
            warnings=[],
        )

    def _simulate(self, *, weights: pd.DataFrame, returns: pd.DataFrame, initial_cash: float, cost_rate: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        current_weights = pd.Series(0.0, index=weights.columns, dtype=float)
        value = float(initial_cash)
        equity_rows: list[dict[str, Any]] = []
        order_rows: list[dict[str, Any]] = []
        holding_rows: list[dict[str, Any]] = []
        for idx, date in enumerate(weights.index):
            daily_returns = returns.loc[date].fillna(0.0)
            gross_return = float(current_weights.mul(daily_returns, fill_value=0.0).sum())
            start_value = value
            value_before_trade = value * (1.0 + gross_return)
            drifted_weights = current_weights * (1.0 + daily_returns)
            if value_before_trade > 0:
                drifted_weights = drifted_weights / (1.0 + gross_return)
            drifted_weights = drifted_weights.fillna(0.0).clip(lower=0.0)

            target_weights = weights.loc[date].fillna(0.0).clip(lower=0.0)
            delta = target_weights.subtract(drifted_weights, fill_value=0.0)
            turnover = float(delta.abs().sum())
            transaction_cost = value_before_trade * turnover * cost_rate
            value_after_trade = max(value_before_trade - transaction_cost, 0.0)
            net_return = value_after_trade / start_value - 1.0 if start_value else 0.0
            cash_weight = max(0.0, 1.0 - float(target_weights.sum()))

            for symbol, change in delta.items():
                if abs(float(change)) > 1e-10:
                    order_rows.append(
                        {
                            "datetime": date,
                            "symbol": symbol,
                            "side": "buy" if change > 0 else "sell",
                            "weight_change": float(change),
                            "trade_value": float(abs(change) * value_before_trade),
                            "transaction_cost": float(abs(change) * value_before_trade * cost_rate),
                            "weight_before": float(drifted_weights.get(symbol, 0.0)),
                            "weight_after": float(target_weights.get(symbol, 0.0)),
                        }
                    )
            for symbol, weight in target_weights.items():
                if weight > 0:
                    holding_rows.append(
                        {
                            "datetime": date,
                            "symbol": symbol,
                            "weight": float(weight),
                            "market_value": float(weight * value_after_trade),
                        }
                    )
            equity_rows.append(
                {
                    "datetime": date,
                    "portfolio_value": float(value_after_trade),
                    "return": float(net_return),
                    "gross_return": float(gross_return),
                    "transaction_cost": float(transaction_cost),
                    "turnover": turnover,
                    "gross_exposure": float(target_weights.sum()),
                    "cash_weight": cash_weight,
                    "holding_count": int((target_weights > 0).sum()),
                }
            )
            value = value_after_trade
            current_weights = target_weights
        return pd.DataFrame(equity_rows), pd.DataFrame(order_rows), pd.DataFrame(holding_rows)

    def _build_benchmark_curve(self, *, returns: pd.DataFrame, initial_cash: float, benchmark: str, cost_rate: float) -> pd.DataFrame:
        benchmark = benchmark or "equal_weight_rebalance"
        if benchmark == "cash":
            ret = pd.Series(0.0, index=returns.index)
            turnover = pd.Series(0.0, index=returns.index)
        elif benchmark == "equal_weight_buy_hold":
            first_available = returns.notna().idxmax()
            start_assets = [symbol for symbol in returns.columns if pd.notna(first_available.get(symbol)) and first_available.get(symbol) == returns.index[0]]
            if not start_assets:
                start_assets = list(returns.columns)
            current_weights = pd.Series(0.0, index=returns.columns, dtype=float)
            current_weights.loc[start_assets] = 1.0 / len(start_assets)
            ret_values = []
            turnover_values = []
            for date in returns.index:
                daily = returns.loc[date].fillna(0.0)
                gross_return = float(current_weights.mul(daily, fill_value=0.0).sum())
                ret_values.append(gross_return)
                turnover_values.append(0.0)
                current_weights = current_weights * (1.0 + daily)
                if 1.0 + gross_return > 0:
                    current_weights = current_weights / (1.0 + gross_return)
            ret = pd.Series(ret_values, index=returns.index)
            turnover = pd.Series(turnover_values, index=returns.index)
        elif benchmark == "simple_momentum_topk":
            ret, turnover = self._simple_momentum_topk_benchmark(returns)
        else:
            ret, turnover = self._equal_weight_rebalance_benchmark(returns)
            benchmark = "equal_weight_rebalance"
        net_ret = ret - turnover * cost_rate
        value = float(initial_cash) * (1.0 + net_ret).cumprod()
        return pd.DataFrame(
            {
                "datetime": returns.index,
                "benchmark_name": benchmark,
                "benchmark_value": value.values,
                "benchmark_return": net_ret.values,
                "benchmark_turnover": turnover.values,
                "benchmark_cum_return": value.values / float(initial_cash) - 1.0,
            }
        )

    def _equal_weight_rebalance_benchmark(self, returns: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        ret_values = []
        turnover_values = []
        prev = pd.Series(0.0, index=returns.columns, dtype=float)
        for date in returns.index:
            available = returns.loc[date].notna()
            target = pd.Series(0.0, index=returns.columns, dtype=float)
            if available.any():
                target.loc[available.index[available]] = 1.0 / int(available.sum())
            ret_values.append(float(prev.mul(returns.loc[date].fillna(0.0), fill_value=0.0).sum()))
            turnover_values.append(float(target.subtract(prev, fill_value=0.0).abs().sum()))
            prev = target
        return pd.Series(ret_values, index=returns.index), pd.Series(turnover_values, index=returns.index)

    def _simple_momentum_topk_benchmark(self, returns: pd.DataFrame, *, window: int = 60, top_k: int = 4) -> tuple[pd.Series, pd.Series]:
        ret_values = []
        turnover_values = []
        prev = pd.Series(0.0, index=returns.columns, dtype=float)
        wealth = (1.0 + returns.fillna(0.0)).cumprod()
        for idx, date in enumerate(returns.index):
            if idx < window:
                target = pd.Series(0.0, index=returns.columns, dtype=float)
            else:
                momentum = wealth.iloc[idx - 1] / wealth.iloc[max(0, idx - window)] - 1.0
                selected = momentum.sort_values(ascending=False).head(top_k)
                selected = selected[selected > 0]
                target = pd.Series(0.0, index=returns.columns, dtype=float)
                if not selected.empty:
                    target.loc[selected.index] = 1.0 / len(selected)
            ret_values.append(float(prev.mul(returns.loc[date].fillna(0.0), fill_value=0.0).sum()))
            turnover_values.append(float(target.subtract(prev, fill_value=0.0).abs().sum()))
            prev = target
        return pd.Series(ret_values, index=returns.index), pd.Series(turnover_values, index=returns.index)

    def _attach_benchmark(self, equity: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
        merged = equity.merge(benchmark, on="datetime", how="left")
        merged["excess_return"] = merged["return"].fillna(0.0) - merged["benchmark_return"].fillna(0.0)
        merged["cum_return"] = merged["portfolio_value"] / float(merged["portfolio_value"].iloc[0]) - 1.0
        merged["excess_cum_return"] = (1.0 + merged["return"].fillna(0.0)).cumprod() / (1.0 + merged["benchmark_return"].fillna(0.0)).cumprod() - 1.0
        return merged

    def _compute_metrics(self, *, equity: pd.DataFrame, orders: pd.DataFrame, holdings: pd.DataFrame, initial_cash: float, benchmark_name: str) -> dict[str, Any]:
        values = equity["portfolio_value"].astype(float)
        returns = equity["return"].astype(float).fillna(0.0)
        benchmark_values = equity["benchmark_value"].astype(float)
        benchmark_returns = equity["benchmark_return"].astype(float).fillna(0.0)
        total_return = float(values.iloc[-1] / initial_cash - 1.0)
        benchmark_total_return = float(benchmark_values.iloc[-1] / initial_cash - 1.0)
        periods = max(len(equity), 1)
        annual_return = (1.0 + total_return) ** (252.0 / periods) - 1.0 if total_return > -1 else -1.0
        benchmark_annual_return = (1.0 + benchmark_total_return) ** (252.0 / periods) - 1.0 if benchmark_total_return > -1 else -1.0
        annual_vol = float(returns.std(ddof=0) * math.sqrt(252.0))
        benchmark_annual_vol = float(benchmark_returns.std(ddof=0) * math.sqrt(252.0))
        sharpe = float(returns.mean() / returns.std(ddof=0) * math.sqrt(252.0)) if returns.std(ddof=0) > 0 else 0.0
        benchmark_sharpe = float(benchmark_returns.mean() / benchmark_returns.std(ddof=0) * math.sqrt(252.0)) if benchmark_returns.std(ddof=0) > 0 else 0.0
        dd = values / values.cummax() - 1.0
        benchmark_dd = benchmark_values / benchmark_values.cummax() - 1.0
        return {
            "initial_cash": float(initial_cash),
            "final_value": float(values.iloc[-1]),
            "total_return": total_return,
            "annual_return": annual_return,
            "annual_volatility": annual_vol,
            "sharpe": sharpe,
            "max_drawdown": float(dd.min()),
            "benchmark_name": benchmark_name,
            "benchmark_final_value": float(benchmark_values.iloc[-1]),
            "benchmark_total_return": benchmark_total_return,
            "benchmark_annual_return": benchmark_annual_return,
            "benchmark_annual_volatility": benchmark_annual_vol,
            "benchmark_sharpe": benchmark_sharpe,
            "benchmark_max_drawdown": float(benchmark_dd.min()),
            "excess_total_return": total_return - benchmark_total_return,
            "average_turnover": float(equity["turnover"].mean()),
            "total_turnover": float(equity["turnover"].sum()),
            "total_transaction_cost": float(equity["transaction_cost"].sum()),
            "average_gross_exposure": float(equity["gross_exposure"].mean()),
            "average_holding_count": float(equity["holding_count"].mean()),
            "order_count": int(len(orders)),
            "holding_record_count": int(len(holdings)),
            "start_date": str(pd.Timestamp(equity["datetime"].min()).date()),
            "end_date": str(pd.Timestamp(equity["datetime"].max()).date()),
            "bar_count": int(len(equity)),
        }

    def _format_report(self, metrics: dict[str, Any]) -> str:
        keys = [
            "backtest_id",
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
            "total_transaction_cost",
            "average_gross_exposure",
            "average_holding_count",
        ]
        lines = ["# Budget Backtest Report", ""]
        for key in keys:
            lines.append(f"- {key}: {metrics.get(key)}")
        lines.append("")
        return "\n".join(lines)

    def _write_comparison_chart(self, equity: pd.DataFrame, output_path: Path) -> None:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception:
            return
        chart = equity.copy()
        chart["datetime"] = pd.to_datetime(chart["datetime"])
        strategy_norm = chart["portfolio_value"] / chart["portfolio_value"].iloc[0]
        benchmark_norm = chart["benchmark_value"] / chart["benchmark_value"].iloc[0]
        strategy_dd = chart["portfolio_value"] / chart["portfolio_value"].cummax() - 1.0
        benchmark_dd = chart["benchmark_value"] / chart["benchmark_value"].cummax() - 1.0
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
        ax1.plot(chart["datetime"], strategy_norm, label="Budget Policy", linewidth=1.6)
        ax1.plot(chart["datetime"], benchmark_norm, label="Benchmark", linewidth=1.3)
        ax1.set_title("Budget Policy vs Benchmark")
        ax1.grid(True, alpha=0.25)
        ax1.legend()
        ax2.plot(chart["datetime"], strategy_dd, label="Budget DD", linewidth=1.1)
        ax2.plot(chart["datetime"], benchmark_dd, label="Benchmark DD", linewidth=1.1)
        ax2.fill_between(chart["datetime"], strategy_dd, 0, alpha=0.16)
        ax2.grid(True, alpha=0.25)
        ax2.legend()
        fig.tight_layout()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=160)
        plt.close(fig)

    def _load_policy_execution_manifest(self, path: Path | None) -> dict[str, Any]:
        if path is None:
            return {}
        resolved = self._resolve_path(path)
        data = json.loads(resolved.read_text(encoding="utf-8"))
        data["_path"] = resolved
        return data

    def _resolve_weights_path(self, weights_path: Path | None, manifest: dict[str, Any]) -> Path:
        if weights_path is not None:
            return self._resolve_path(weights_path)
        path = manifest.get("daily_budget_weights_path")
        if not path:
            raise ValueError("必须提供 --weights-path 或 --policy-execution-manifest-path。")
        return self._resolve_path(path)

    def _resolve_returns_path(self, state: dict[str, Any], override: Path | None) -> Path:
        if override is not None:
            return self._resolve_path(override)
        path = state.get("data_panel", {}).get("returns_wide")
        if not path:
            raise ValueError("budget_run_state.json 中缺少 data_panel.returns_wide。")
        return self._resolve_path(path)

    def _resolve_settings(self, request: BudgetBacktestRequest, state: dict[str, Any]) -> dict[str, float]:
        base = dict(state.get("backtest_config") or {})
        return {
            "initial_cash": float(request.initial_cash if request.initial_cash is not None else base.get("initial_cash", 100000)),
            "commission": float(request.commission if request.commission is not None else base.get("commission", 0.0001)),
            "slippage_perc": float(request.slippage_perc if request.slippage_perc is not None else base.get("slippage_perc", 0.0001)),
        }

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any], backtest_id: str) -> Path:
        if output_dir is not None:
            return self._resolve_path(output_dir)
        policies_dir = state.get("directories", {}).get("policies")
        if policies_dir:
            return self._resolve_path(policies_dir) / "backtests" / backtest_id
        return self.config.root_dir / "artifacts" / "budget_runs" / str(state.get("budget_run_id")) / "policies" / "backtests" / backtest_id

    def _load_weights(self, path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        weights = pd.read_parquet(path)
        weights.index = pd.to_datetime(weights.index).normalize()
        weights.columns = [normalize_symbol(column, reference_symbols=reference_symbols) for column in weights.columns]
        return weights.sort_index().fillna(0.0)

    def _load_returns(self, path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        return load_wide_parquet(path, reference_symbols=reference_symbols)

    def _reference_symbols_from_state(self, state: dict[str, Any]) -> list[str]:
        symbols: list[str] = []
        asset_symbols = state.get("asset_pool", {}).get("symbols")
        if isinstance(asset_symbols, list):
            symbols.extend(str(item).upper() for item in asset_symbols)
        signal_manifest_path = state.get("signal_artifacts", {}).get("manifest_path")
        if signal_manifest_path:
            path = self._resolve_path(signal_manifest_path)
            if path.exists():
                manifest = json.loads(path.read_text(encoding="utf-8-sig"))
                for record in manifest.get("records", []):
                    if record.get("symbol"):
                        symbols.append(str(record["symbol"]).upper())
        return sorted({item for item in symbols if item})

    def _align_weights_and_returns(self, weights: pd.DataFrame, returns: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        idx = weights.index.intersection(returns.index)
        columns = sorted(set(weights.columns).intersection(returns.columns))
        if len(idx) == 0 or not columns:
            raise ValueError("weights 和 returns 没有可对齐的日期或资产。")
        return weights.loc[idx, columns].sort_index(), returns.loc[idx, columns].sort_index()

    def _make_backtest_id(self, manifest: dict[str, Any], weights_path: Path) -> str:
        base = str(manifest.get("policy_id") or weights_path.parent.name or "budget_policy")
        safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in base).strip("_") or "budget_policy"
        return f"{safe}_bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def _update_run_state(self, *, state_path: Path, state: dict[str, Any], backtest_id: str, manifest: dict[str, Any], manifest_path: Path) -> None:
        now = datetime.now().isoformat()
        state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("backtests", {})[backtest_id] = {
            "manifest_path": str(self._relative(manifest_path)),
            "equity_curve": manifest["equity_curve_path"],
            "benchmark_curve": manifest["benchmark_curve_path"],
            "orders": manifest["orders_path"],
            "holdings": manifest["holdings_path"],
            "metrics": manifest["metrics_path"],
            "report": manifest["report_path"],
            "comparison_chart": manifest.get("comparison_chart_path"),
            "summary": manifest["metrics"],
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetBacktestService",
                "event": "budget_policy_backtest_completed",
                "summary": f"预算策略组合回测已完成：{backtest_id}",
                "backtest_id": backtest_id,
                "manifest_path": str(self._relative(manifest_path)),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

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
