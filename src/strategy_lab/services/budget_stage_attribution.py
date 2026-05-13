from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_run import BudgetRunManager


class BudgetStageAttributionRequest(BaseModel):
    budget_run_state_path: Path
    search_id: str | None = None
    search_result_path: Path | None = None
    profile_path: Path | None = None
    output_dir: Path | None = None
    generate_chart: bool = True
    update_run_state: bool = True

    @model_validator(mode="after")
    def _validate_locator(self) -> "BudgetStageAttributionRequest":
        if not self.search_id and not self.search_result_path:
            raise ValueError("必须提供 search_id 或 search_result_path。")
        return self


class BudgetStageAttributionResult(BaseModel):
    budget_run_state_path: Path
    search_id: str
    output_dir: Path
    json_path: Path
    csv_path: Path
    markdown_path: Path
    chart_path: Path | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class BudgetStageAttributionService:
    """预算层市场阶段归因服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetStageAttributionRequest) -> BudgetStageAttributionResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        search_result_path = self._resolve_search_result_path(request, state=state)
        search_result = self._read_json(search_result_path)
        search_id = str(search_result.get("search_id") or request.search_id or search_result_path.parent.name)
        search_dir = search_result_path.parent
        output_dir = self._resolve_output_dir(request.output_dir, search_dir=search_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        profile_path = self._resolve_profile_path(request.profile_path, state=state)
        profile = self._read_json(profile_path)
        backtest_manifest_path = self._path_from_result(
            search_result,
            "best_backtest_manifest_path",
            search_dir / "best" / "full" / "backtest" / "budget_backtest_manifest.json",
        )
        backtest_manifest = self._read_json(backtest_manifest_path)
        equity_path = self._resolve_path(backtest_manifest["equity_curve_path"])
        orders_path = self._resolve_path(backtest_manifest["orders_path"])
        holdings_path = self._resolve_path(backtest_manifest["holdings_path"])
        metrics_path = self._resolve_path(backtest_manifest["metrics_path"])

        equity = pd.read_parquet(equity_path)
        orders = pd.read_parquet(orders_path) if orders_path.exists() else pd.DataFrame()
        holdings = pd.read_parquet(holdings_path) if holdings_path.exists() else pd.DataFrame()
        metrics = self._read_json_if_exists(metrics_path)
        rows = self._build_stage_rows(
            profile=profile,
            equity=equity,
            orders=orders,
            holdings=holdings,
        )
        summary = self._build_summary(
            state=state,
            search_id=search_id,
            search_result=search_result,
            rows=rows,
            metrics=metrics,
            source_paths={
                "profile_path": str(self._relative(profile_path)),
                "search_result_path": str(self._relative(search_result_path)),
                "backtest_manifest_path": str(self._relative(backtest_manifest_path)),
                "equity_curve_path": str(self._relative(equity_path)),
                "orders_path": str(self._relative(orders_path)),
                "holdings_path": str(self._relative(holdings_path)),
                "metrics_path": str(self._relative(metrics_path)),
            },
        )

        json_path = output_dir / "stage_attribution.json"
        csv_path = output_dir / "stage_attribution.csv"
        markdown_path = output_dir / "stage_attribution.md"
        chart_path = output_dir / "stage_return_comparison.png"
        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._stage_csv_frame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")
        markdown_path.write_text(self._format_markdown(summary), encoding="utf-8")
        actual_chart_path = None
        if request.generate_chart:
            self._write_chart(pd.DataFrame(rows), chart_path)
            actual_chart_path = chart_path if chart_path.exists() else None

        if request.update_run_state:
            self._update_run_state(
                state_path=state_path,
                state=state,
                search_id=search_id,
                json_path=json_path,
                csv_path=csv_path,
                markdown_path=markdown_path,
                chart_path=actual_chart_path,
                summary=summary,
            )

        return BudgetStageAttributionResult(
            budget_run_state_path=state_path,
            search_id=search_id,
            output_dir=output_dir,
            json_path=json_path,
            csv_path=csv_path,
            markdown_path=markdown_path,
            chart_path=actual_chart_path,
            summary=summary,
        )

    def _build_stage_rows(
        self,
        *,
        profile: dict[str, Any],
        equity: pd.DataFrame,
        orders: pd.DataFrame,
        holdings: pd.DataFrame,
    ) -> list[dict[str, Any]]:
        equity = equity.copy()
        equity["datetime"] = pd.to_datetime(equity["datetime"])
        orders = orders.copy()
        if not orders.empty and "datetime" in orders:
            orders["datetime"] = pd.to_datetime(orders["datetime"])
        holdings = holdings.copy()
        if not holdings.empty and "datetime" in holdings:
            holdings["datetime"] = pd.to_datetime(holdings["datetime"])

        rows: list[dict[str, Any]] = []
        for index, segment in enumerate(profile.get("regime_segments", []), start=1):
            start = pd.to_datetime(segment.get("start"))
            end = pd.to_datetime(segment.get("end"))
            segment_equity = equity[(equity["datetime"] >= start) & (equity["datetime"] <= end)].copy()
            if segment_equity.empty:
                continue
            segment_orders = orders[(orders["datetime"] >= start) & (orders["datetime"] <= end)].copy() if not orders.empty else orders
            segment_holdings = holdings[(holdings["datetime"] >= start) & (holdings["datetime"] <= end)].copy() if not holdings.empty else holdings
            strategy_return = self._period_return(segment_equity, "portfolio_value")
            benchmark_return = self._period_return(segment_equity, "benchmark_value")
            row = {
                "stage_id": f"stage_{index:03d}",
                "start": str(start.date()),
                "end": str(end.date()),
                "trading_days": int(len(segment_equity)),
                "market_label": segment.get("label"),
                "market_equal_weight_return": self._safe_float(segment.get("equal_weight_return")),
                "market_max_drawdown": self._safe_float(segment.get("max_drawdown")),
                "market_average_correlation": self._safe_float(segment.get("average_correlation")),
                "market_average_volatility": self._safe_float(segment.get("average_volatility")),
                "market_description": segment.get("description"),
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "excess_return": strategy_return - benchmark_return,
                "strategy_max_drawdown": self._max_drawdown(segment_equity, "portfolio_value"),
                "benchmark_max_drawdown": self._max_drawdown(segment_equity, "benchmark_value"),
                "strategy_volatility": self._annual_volatility(segment_equity, "return"),
                "benchmark_volatility": self._annual_volatility(segment_equity, "benchmark_return"),
                "information_ratio": self._information_ratio(segment_equity),
                "outperform_day_ratio": self._outperform_day_ratio(segment_equity),
                "average_turnover": self._mean(segment_equity, "turnover"),
                "total_turnover": self._sum(segment_equity, "turnover"),
                "total_transaction_cost": self._sum(segment_equity, "transaction_cost"),
                "average_gross_exposure": self._mean(segment_equity, "gross_exposure"),
                "average_cash_weight": self._mean(segment_equity, "cash_weight"),
                "average_holding_count": self._mean(segment_equity, "holding_count"),
                "max_holding_count": self._max(segment_equity, "holding_count"),
                "order_summary": self._order_summary(segment_orders),
                "top_traded_symbols": self._top_traded_symbols(segment_orders),
                "top_held_symbols": self._top_held_symbols(segment_holdings),
                "orders_sample": self._orders_sample(segment_orders),
            }
            rows.append(row)
        return rows

    def _build_summary(
        self,
        *,
        state: dict[str, Any],
        search_id: str,
        search_result: dict[str, Any],
        rows: list[dict[str, Any]],
        metrics: dict[str, Any],
        source_paths: dict[str, str],
    ) -> dict[str, Any]:
        stage_df = pd.DataFrame(rows)
        best_stage: dict[str, Any] = {}
        worst_stage: dict[str, Any] = {}
        if not stage_df.empty:
            best_stage = stage_df.sort_values("excess_return", ascending=False).iloc[0].to_dict()
            worst_stage = stage_df.sort_values("excess_return", ascending=True).iloc[0].to_dict()
        return {
            "schema_version": "0.1.0",
            "created_at": datetime.now().isoformat(),
            "budget_run_id": state.get("budget_run_id"),
            "search_id": search_id,
            "policy": {
                "best_policy_config_path": search_result.get("best_policy_config_path"),
                "best_params": search_result.get("best_params"),
                "best_score": search_result.get("best_score"),
            },
            "source_paths": source_paths,
            "full_metrics": {
                "total_return": metrics.get("total_return"),
                "benchmark_total_return": metrics.get("benchmark_total_return"),
                "excess_total_return": metrics.get("excess_total_return"),
                "annual_return": metrics.get("annual_return"),
                "sharpe": metrics.get("sharpe"),
                "max_drawdown": metrics.get("max_drawdown"),
                "average_turnover": metrics.get("average_turnover"),
                "average_gross_exposure": metrics.get("average_gross_exposure"),
                "average_holding_count": metrics.get("average_holding_count"),
                "total_transaction_cost": metrics.get("total_transaction_cost"),
            },
            "stage_count": len(rows),
            "best_excess_stage": best_stage,
            "worst_excess_stage": worst_stage,
            "stages": rows,
        }

    def _format_markdown(self, summary: dict[str, Any]) -> str:
        lines = [
            "# Budget Stage Attribution",
            "",
            f"- budget_run_id: {summary.get('budget_run_id')}",
            f"- search_id: {summary.get('search_id')}",
            f"- stage_count: {summary.get('stage_count')}",
            f"- best_score: {summary.get('policy', {}).get('best_score')}",
            "",
            "## Full Metrics",
            "",
        ]
        for key, value in summary.get("full_metrics", {}).items():
            lines.append(f"- {key}: {value}")
        best = summary.get("best_excess_stage") or {}
        worst = summary.get("worst_excess_stage") or {}
        lines.extend(
            [
                "",
                "## Best / Worst Excess Stage",
                "",
                f"- Best: {best.get('stage_id')} {best.get('start')} to {best.get('end')}, excess={best.get('excess_return')}",
                f"- Worst: {worst.get('stage_id')} {worst.get('start')} to {worst.get('end')}, excess={worst.get('excess_return')}",
                "",
                "## Stage Table",
                "",
                "| stage | period | label | days | strategy | benchmark | excess | max_dd | avg_turnover | gross_exposure | holding_count | cost |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in summary.get("stages", []):
            lines.append(
                "| {stage} | {start} to {end} | {label} | {days} | {strategy} | {benchmark} | {excess} | {dd} | {turnover} | {exposure} | {holding_count} | {cost} |".format(
                    stage=item.get("stage_id"),
                    start=item.get("start"),
                    end=item.get("end"),
                    label=item.get("market_label"),
                    days=item.get("trading_days"),
                    strategy=self._fmt(item.get("strategy_return")),
                    benchmark=self._fmt(item.get("benchmark_return")),
                    excess=self._fmt(item.get("excess_return")),
                    dd=self._fmt(item.get("strategy_max_drawdown")),
                    turnover=self._fmt(item.get("average_turnover")),
                    exposure=self._fmt(item.get("average_gross_exposure")),
                    holding_count=self._fmt(item.get("average_holding_count")),
                    cost=self._fmt_number(item.get("total_transaction_cost")),
                )
            )
        lines.extend(["", "## Holdings And Trades By Stage", ""])
        for item in summary.get("stages", []):
            lines.append(f"### {item.get('stage_id')} {item.get('start')} to {item.get('end')}")
            lines.append("")
            lines.append(f"- Market: {item.get('market_description')}")
            order_summary = item.get("order_summary") or {}
            lines.append(
                f"- Orders: {order_summary.get('order_count')}, buy={order_summary.get('buy_count')}, sell={order_summary.get('sell_count')}, gross_trade_value={self._fmt_number(order_summary.get('gross_trade_value'))}"
            )
            lines.append("- Top held symbols: " + self._format_symbol_items(item.get("top_held_symbols") or [], "average_weight"))
            lines.append("- Top traded symbols: " + self._format_symbol_items(item.get("top_traded_symbols") or [], "gross_trade_value"))
            lines.append("")
        lines.extend(["## Source Paths", ""])
        for key, path in summary.get("source_paths", {}).items():
            lines.append(f"- {key}: {path}")
        lines.append("")
        return "\n".join(lines)

    def _stage_csv_frame(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        flat = []
        for row in rows:
            item = {key: value for key, value in row.items() if key not in {"order_summary", "top_traded_symbols", "top_held_symbols", "orders_sample"}}
            item["order_count"] = (row.get("order_summary") or {}).get("order_count")
            item["buy_count"] = (row.get("order_summary") or {}).get("buy_count")
            item["sell_count"] = (row.get("order_summary") or {}).get("sell_count")
            item["gross_trade_value"] = (row.get("order_summary") or {}).get("gross_trade_value")
            item["top_held_symbols"] = json.dumps(row.get("top_held_symbols") or [], ensure_ascii=False)
            item["top_traded_symbols"] = json.dumps(row.get("top_traded_symbols") or [], ensure_ascii=False)
            flat.append(item)
        return pd.DataFrame(flat)

    def _write_chart(self, df: pd.DataFrame, output_path: Path) -> None:
        if df.empty:
            return
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception:
            return
        labels = df["stage_id"].astype(str)
        x = range(len(df))
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
        ax1.bar([i - 0.22 for i in x], df["strategy_return"], width=0.22, label="Budget Policy", color="#2563eb")
        ax1.bar(x, df["benchmark_return"], width=0.22, label="Benchmark", color="#f97316")
        ax1.bar([i + 0.22 for i in x], df["excess_return"], width=0.22, label="Excess", color="#16a34a")
        ax1.axhline(0, color="#111827", linewidth=0.8)
        ax1.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
        ax1.set_title("Budget Stage Return Attribution", loc="left", fontsize=13, fontweight="bold")
        ax1.grid(True, axis="y", alpha=0.25)
        ax1.legend(frameon=False)
        ax2.plot(list(x), df["average_gross_exposure"], marker="o", label="Gross Exposure", color="#7c3aed")
        ax2.plot(list(x), df["average_turnover"], marker="o", label="Avg Turnover", color="#0891b2")
        ax2.set_xticks(list(x))
        ax2.set_xticklabels(labels, rotation=35, ha="right")
        ax2.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
        ax2.grid(True, axis="y", alpha=0.25)
        ax2.legend(frameon=False)
        fig.tight_layout()
        fig.savefig(output_path, dpi=160, bbox_inches="tight")
        plt.close(fig)

    def _order_summary(self, orders: pd.DataFrame) -> dict[str, Any]:
        if orders.empty:
            return {
                "order_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "gross_trade_value": 0.0,
                "total_transaction_cost": 0.0,
                "first_order_date": None,
                "last_order_date": None,
            }
        dates = pd.to_datetime(orders["datetime"])
        side = orders["side"].astype(str).str.lower()
        return {
            "order_count": int(len(orders)),
            "buy_count": int((side == "buy").sum()),
            "sell_count": int((side == "sell").sum()),
            "gross_trade_value": self._sum(orders, "trade_value"),
            "total_transaction_cost": self._sum(orders, "transaction_cost"),
            "first_order_date": str(dates.min().date()),
            "last_order_date": str(dates.max().date()),
        }

    def _top_traded_symbols(self, orders: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
        if orders.empty:
            return []
        grouped = (
            orders.assign(abs_trade_value=pd.to_numeric(orders["trade_value"], errors="coerce").abs().fillna(0.0))
            .groupby("symbol", as_index=False)
            .agg(
                gross_trade_value=("abs_trade_value", "sum"),
                order_count=("symbol", "size"),
                buy_count=("side", lambda item: int((item.astype(str).str.lower() == "buy").sum())),
                sell_count=("side", lambda item: int((item.astype(str).str.lower() == "sell").sum())),
            )
            .sort_values("gross_trade_value", ascending=False)
            .head(limit)
        )
        return grouped.to_dict(orient="records")

    def _top_held_symbols(self, holdings: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
        if holdings.empty:
            return []
        grouped = (
            holdings.assign(weight=pd.to_numeric(holdings["weight"], errors="coerce").fillna(0.0))
            .groupby("symbol", as_index=False)
            .agg(average_weight=("weight", "mean"), max_weight=("weight", "max"), holding_days=("datetime", "nunique"))
            .sort_values("average_weight", ascending=False)
            .head(limit)
        )
        return grouped.to_dict(orient="records")

    def _orders_sample(self, orders: pd.DataFrame, limit: int = 20) -> list[dict[str, Any]]:
        if orders.empty:
            return []
        sample = orders.sort_values("datetime").head(limit).copy()
        sample["datetime"] = pd.to_datetime(sample["datetime"]).dt.strftime("%Y-%m-%d")
        return sample.to_dict(orient="records")

    def _period_return(self, df: pd.DataFrame, column: str) -> float:
        values = pd.to_numeric(df[column], errors="coerce").dropna()
        if len(values) < 2 or float(values.iloc[0]) == 0:
            return 0.0
        return float(values.iloc[-1] / values.iloc[0] - 1.0)

    def _max_drawdown(self, df: pd.DataFrame, column: str) -> float:
        values = pd.to_numeric(df[column], errors="coerce").dropna()
        if values.empty:
            return 0.0
        return float((values / values.cummax() - 1.0).min())

    def _annual_volatility(self, df: pd.DataFrame, column: str) -> float:
        if column not in df:
            return 0.0
        returns = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
        return float(returns.std(ddof=0) * math.sqrt(252.0))

    def _information_ratio(self, df: pd.DataFrame) -> float | None:
        if "excess_return" not in df:
            return None
        excess = pd.to_numeric(df["excess_return"], errors="coerce").fillna(0.0)
        std = excess.std(ddof=0)
        if std <= 0:
            return None
        return float(excess.mean() / std * math.sqrt(252.0))

    def _outperform_day_ratio(self, df: pd.DataFrame) -> float | None:
        if "return" not in df or "benchmark_return" not in df:
            return None
        return float((df["return"] > df["benchmark_return"]).mean())

    def _mean(self, df: pd.DataFrame, column: str) -> float:
        if column not in df or df.empty:
            return 0.0
        return float(pd.to_numeric(df[column], errors="coerce").fillna(0.0).mean())

    def _sum(self, df: pd.DataFrame, column: str) -> float:
        if column not in df or df.empty:
            return 0.0
        return float(pd.to_numeric(df[column], errors="coerce").fillna(0.0).sum())

    def _max(self, df: pd.DataFrame, column: str) -> float:
        if column not in df or df.empty:
            return 0.0
        return float(pd.to_numeric(df[column], errors="coerce").fillna(0.0).max())

    def _resolve_search_result_path(self, request: BudgetStageAttributionRequest, *, state: dict[str, Any]) -> Path:
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

    def _resolve_profile_path(self, override: Path | None, *, state: dict[str, Any]) -> Path:
        if override:
            return self._resolve_path(override)
        profile = state.get("artifacts", {}).get("profile", {}).get("budget_profile", {})
        raw = profile.get("profile_json") or profile.get("json_path") or state.get("budget_profile", {}).get("profile_json")
        if not raw:
            raise FileNotFoundError("budget_run_state.json 缺少预算层画像 JSON 路径。")
        return self._resolve_path(raw)

    def _resolve_output_dir(self, output_dir: Path | None, *, search_dir: Path) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        return search_dir / "stage_attribution"

    def _path_from_result(self, search_result: dict[str, Any], key: str, default: Path) -> Path:
        value = search_result.get(key)
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
        json_path: Path,
        csv_path: Path,
        markdown_path: Path,
        chart_path: Path | None,
        summary: dict[str, Any],
    ) -> None:
        entry = state.setdefault("artifacts", {}).setdefault("policies", {}).setdefault("searches", {}).setdefault(search_id, {})
        entry.update(
            {
                "stage_attribution_path": str(self._relative(json_path)),
                "stage_attribution_csv_path": str(self._relative(csv_path)),
                "stage_attribution_md_path": str(self._relative(markdown_path)),
                "stage_attribution_chart_path": str(self._relative(chart_path)) if chart_path else None,
                "stage_attribution_summary": {
                    "stage_count": summary.get("stage_count"),
                    "best_excess_stage": (summary.get("best_excess_stage") or {}).get("stage_id"),
                    "worst_excess_stage": (summary.get("worst_excess_stage") or {}).get("stage_id"),
                },
            }
        )
        now = datetime.now().isoformat()
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetStageAttributionService",
                "event": "budget_stage_attribution_created",
                "summary": f"预算层搜索 {search_id} 已生成市场阶段归因。",
                "search_id": search_id,
                "stage_attribution_path": str(self._relative(json_path)),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _format_symbol_items(self, items: list[dict[str, Any]], value_key: str) -> str:
        if not items:
            return "None"
        return ", ".join(f"{item.get('symbol')}={self._fmt(item.get(value_key))}" for item in items[:8])

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

    def _fmt_number(self, value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "" if value is None else str(value)
        if pd.isna(number):
            return ""
        return f"{number:.2f}"

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: Path | None) -> Path | str:
        if path is None:
            return ""
        try:
            return path.resolve().relative_to(self.config.root_dir.resolve())
        except ValueError:
            return path
