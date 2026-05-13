from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.signal_run import SignalRunManager


class StageAttributionRequest(BaseModel):
    run_state_path: Path
    attempt_id: str
    output_dir: Path | None = None
    generate_chart: bool = True


class StageAttributionResult(BaseModel):
    run_state_path: Path
    attempt_id: str
    output_dir: Path
    json_path: Path
    csv_path: Path
    markdown_path: Path
    chart_path: Path | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


class StageAttributionService:
    """按市场画像阶段归因策略表现。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)

    def run(self, request: StageAttributionRequest) -> StageAttributionResult:
        state = self.run_manager.load_state(request.run_state_path)
        attempt = self._find_attempt(state, request.attempt_id)
        output_dir = self._resolve_output_dir(request.output_dir, attempt)
        output_dir.mkdir(parents=True, exist_ok=True)

        market_profile_path = self._market_profile_path(state)
        equity_path = self._resolve_path(attempt["full_backtest_dir"]) / "equity_curve.parquet"
        orders_path = self._resolve_path(attempt["full_backtest_dir"]) / "orders.parquet"
        signals_path = self._resolve_path(attempt["full_backtest_dir"]) / "daily_signals.parquet"
        metrics_path = self._resolve_path(attempt["full_backtest_dir"]) / "metrics.json"

        market_profile = json.loads(market_profile_path.read_text(encoding="utf-8"))
        equity_df = pd.read_parquet(equity_path)
        orders_df = pd.read_parquet(orders_path) if orders_path.exists() else pd.DataFrame()
        signals_df = pd.read_parquet(signals_path) if signals_path.exists() else pd.DataFrame()
        full_metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}

        stages = self._build_stage_rows(
            market_profile=market_profile,
            equity_df=equity_df,
            orders_df=orders_df,
            signals_df=signals_df,
        )
        summary = self._build_summary(
            state=state,
            attempt=attempt,
            stages=stages,
            full_metrics=full_metrics,
            source_paths={
                "market_profile_path": self._relative(market_profile_path),
                "equity_curve_path": self._relative(equity_path),
                "orders_path": self._relative(orders_path),
                "daily_signals_path": self._relative(signals_path),
                "metrics_path": self._relative(metrics_path),
            },
        )

        json_path = output_dir / "stage_attribution.json"
        csv_path = output_dir / "stage_attribution.csv"
        markdown_path = output_dir / "stage_attribution.md"
        chart_path = output_dir / "stage_return_comparison.png"

        json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._stage_csv_frame(stages).to_csv(csv_path, index=False, encoding="utf-8-sig")
        markdown_path.write_text(self._format_markdown(summary), encoding="utf-8")
        actual_chart_path = None
        if request.generate_chart:
            self._write_chart(pd.DataFrame(stages), chart_path)
            actual_chart_path = chart_path if chart_path.exists() else None

        fields = {
            "stage_attribution_dir": self._relative(output_dir),
            "stage_attribution_path": self._relative(json_path),
            "stage_attribution_csv_path": self._relative(csv_path),
            "stage_attribution_md_path": self._relative(markdown_path),
        }
        if actual_chart_path:
            fields["stage_attribution_chart_path"] = self._relative(actual_chart_path)
        self.run_manager.update_attempt(
            request.run_state_path,
            attempt_id=request.attempt_id,
            fields=fields,
        )
        self.run_manager.append_event(
            request.run_state_path,
            actor="StageAttributionService",
            event="stage_attribution_created",
            summary=f"{request.attempt_id} 已生成市场阶段归因分析。",
            extra={"attempt_id": request.attempt_id, "stage_attribution_path": self._relative(json_path)},
        )

        return StageAttributionResult(
            run_state_path=self._resolve_path(request.run_state_path),
            attempt_id=request.attempt_id,
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
        market_profile: dict[str, Any],
        equity_df: pd.DataFrame,
        orders_df: pd.DataFrame,
        signals_df: pd.DataFrame,
    ) -> list[dict[str, Any]]:
        equity = equity_df.copy()
        equity["datetime"] = pd.to_datetime(equity["datetime"])
        orders = orders_df.copy()
        if not orders.empty and "datetime" in orders:
            orders["datetime"] = pd.to_datetime(orders["datetime"])
        signals = signals_df.copy()
        if not signals.empty and "datetime" in signals:
            signals["datetime"] = pd.to_datetime(signals["datetime"])

        rows: list[dict[str, Any]] = []
        for index, segment in enumerate(market_profile.get("regime_segments", []), start=1):
            start = pd.to_datetime(segment.get("start"))
            end = pd.to_datetime(segment.get("end"))
            segment_equity = equity[(equity["datetime"] >= start) & (equity["datetime"] <= end)].copy()
            if segment_equity.empty:
                continue
            segment_orders = orders[(orders["datetime"] >= start) & (orders["datetime"] <= end)].copy() if not orders.empty else orders
            segment_signals = (
                signals[(signals["datetime"] >= start) & (signals["datetime"] <= end)].copy() if not signals.empty else signals
            )
            trade_summary = self._trade_summary(segment_orders)
            trades = self._trade_rows(segment_orders=segment_orders, signals_df=signals)
            rows.append(
                {
                    "stage_id": f"stage_{index:03d}",
                    "start": str(start.date()),
                    "end": str(end.date()),
                    "trading_days": int(len(segment_equity)),
                    "market_label": segment.get("label"),
                    "market_label_zh": segment.get("label_zh"),
                    "market_return": self._safe_float(segment.get("return")),
                    "market_max_drawdown": self._safe_float(segment.get("max_drawdown")),
                    "market_volatility": self._safe_float(segment.get("volatility") or segment.get("annualized_volatility")),
                    "strategy_return": self._period_return(segment_equity, "portfolio_value"),
                    "benchmark_return": self._period_return(segment_equity, "benchmark_value"),
                    "excess_return": self._period_return(segment_equity, "portfolio_value")
                    - self._period_return(segment_equity, "benchmark_value"),
                    "strategy_max_drawdown": self._max_drawdown(segment_equity, "portfolio_value"),
                    "benchmark_max_drawdown": self._max_drawdown(segment_equity, "benchmark_value"),
                    "strategy_volatility": self._annual_volatility(segment_equity, "return"),
                    "benchmark_volatility": self._annual_volatility(segment_equity, "benchmark_return"),
                    "information_ratio": self._information_ratio(segment_equity),
                    "outperform_day_ratio": self._outperform_day_ratio(segment_equity),
                    "order_count": int(len(segment_orders)),
                    "buy_count": trade_summary["buy_count"],
                    "sell_count": trade_summary["sell_count"],
                    "gross_turnover": trade_summary["gross_turnover"],
                    "net_size": trade_summary["net_size"],
                    "avg_trade_value": trade_summary["avg_trade_value"],
                    "first_trade_date": trade_summary["first_trade_date"],
                    "last_trade_date": trade_summary["last_trade_date"],
                    "signal_changes": self._signal_changes(segment_signals),
                    "exposure_mean": self._exposure_mean(segment_signals),
                    "market_description": segment.get("description"),
                    "trade_summary": trade_summary,
                    "trades": trades,
                }
            )
        return rows

    def _build_summary(
        self,
        *,
        state: dict[str, Any],
        attempt: dict[str, Any],
        stages: list[dict[str, Any]],
        full_metrics: dict[str, Any],
        source_paths: dict[str, str],
    ) -> dict[str, Any]:
        stage_df = pd.DataFrame(stages)
        best_stage = {}
        worst_stage = {}
        if not stage_df.empty:
            best_stage = stage_df.sort_values("excess_return", ascending=False).iloc[0].to_dict()
            worst_stage = stage_df.sort_values("excess_return", ascending=True).iloc[0].to_dict()
        return {
            "created_at": datetime.now().isoformat(),
            "run_id": state.get("run_id"),
            "attempt_id": attempt.get("attempt_id"),
            "strategy_name": attempt.get("strategy_name"),
            "source_paths": source_paths,
            "full_metrics": {
                "total_return": full_metrics.get("total_return"),
                "benchmark_total_return": full_metrics.get("benchmark_total_return"),
                "excess_total_return": full_metrics.get("excess_total_return"),
                "sharpe": full_metrics.get("sharpe"),
                "max_drawdown": full_metrics.get("max_drawdown"),
                "information_ratio": full_metrics.get("information_ratio"),
            },
            "stage_count": len(stages),
            "best_excess_stage": best_stage,
            "worst_excess_stage": worst_stage,
            "stages": stages,
        }

    def _format_markdown(self, summary: dict[str, Any]) -> str:
        lines = [
            "# Stage Attribution",
            "",
            f"- Run ID: {summary.get('run_id')}",
            f"- Attempt ID: {summary.get('attempt_id')}",
            f"- Strategy: {summary.get('strategy_name')}",
            f"- Stage Count: {summary.get('stage_count')}",
            "",
            "## Full Metrics",
            "",
        ]
        for key, value in summary.get("full_metrics", {}).items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## Best / Worst Excess Stage", ""])
        best = summary.get("best_excess_stage") or {}
        worst = summary.get("worst_excess_stage") or {}
        lines.append(f"- Best: {best.get('stage_id')} {best.get('start')} to {best.get('end')}, excess={best.get('excess_return')}")
        lines.append(f"- Worst: {worst.get('stage_id')} {worst.get('start')} to {worst.get('end')}, excess={worst.get('excess_return')}")
        lines.extend(["", "## Stage Table", ""])
        lines.append("| stage | period | label | days | strategy | benchmark | excess | max_dd | orders | buy | sell | exposure |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for item in summary.get("stages", []):
            lines.append(
                "| {stage} | {start} to {end} | {label} | {days} | {strategy} | {benchmark} | {excess} | {dd} | {orders} | {buy} | {sell} | {exposure} |".format(
                    stage=item.get("stage_id"),
                    start=item.get("start"),
                    end=item.get("end"),
                    label=item.get("market_label_zh") or item.get("market_label"),
                    days=item.get("trading_days"),
                    strategy=self._fmt(item.get("strategy_return")),
                    benchmark=self._fmt(item.get("benchmark_return")),
                    excess=self._fmt(item.get("excess_return")),
                    dd=self._fmt(item.get("strategy_max_drawdown")),
                    orders=item.get("order_count"),
                    buy=item.get("buy_count"),
                    sell=item.get("sell_count"),
                    exposure=self._fmt(item.get("exposure_mean")),
                )
            )
        lines.extend(["", "## Trades By Stage", ""])
        for item in summary.get("stages", []):
            lines.append(f"### {item.get('stage_id')} {item.get('start')} to {item.get('end')}")
            lines.append("")
            lines.append(f"- Market Label: {item.get('market_label_zh') or item.get('market_label')}")
            lines.append(f"- Orders: {item.get('order_count')}, Buy: {item.get('buy_count')}, Sell: {item.get('sell_count')}")
            lines.append(f"- Gross Turnover: {self._fmt_number(item.get('gross_turnover'))}")
            trades = item.get("trades") or []
            if not trades:
                lines.extend(["", "No completed orders in this stage.", ""])
                continue
            lines.extend(["", "| datetime | side | size | price | value | commission | target before | target after |"])
            lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
            for trade in trades:
                lines.append(
                    "| {datetime} | {side} | {size} | {price} | {value} | {commission} | {before} | {after} |".format(
                        datetime=trade.get("datetime"),
                        side=trade.get("side"),
                        size=self._fmt_number(trade.get("size")),
                        price=self._fmt_number(trade.get("price")),
                        value=self._fmt_number(trade.get("value")),
                        commission=self._fmt_number(trade.get("commission")),
                        before=self._fmt(trade.get("target_signal_before")),
                        after=self._fmt(trade.get("target_signal_after")),
                    )
                )
            lines.append("")
        return "\n".join(lines)

    def _stage_csv_frame(self, stages: list[dict[str, Any]]) -> pd.DataFrame:
        rows = []
        for stage in stages:
            rows.append({key: value for key, value in stage.items() if key not in {"trade_summary", "trades"}})
        return pd.DataFrame(rows)

    def _write_chart(self, df: pd.DataFrame, output_path: Path) -> None:
        if df.empty:
            return
        try:
            import matplotlib

            matplotlib.use("Agg", force=True)
            import matplotlib.pyplot as plt
        except ImportError:
            return
        labels = df["stage_id"].astype(str)
        x = range(len(df))
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.bar([i - 0.2 for i in x], df["strategy_return"], width=0.2, label="Strategy", color="#2563eb")
        ax.bar(x, df["benchmark_return"], width=0.2, label="Benchmark", color="#f97316")
        ax.bar([i + 0.2 for i in x], df["excess_return"], width=0.2, label="Excess", color="#16a34a")
        ax.axhline(0, color="#111827", linewidth=0.8)
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=35, ha="right")
        ax.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
        ax.set_title("Stage Return Attribution", loc="left", fontsize=13, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.25)
        ax.legend(frameon=False)
        fig.tight_layout()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=160, bbox_inches="tight")
        plt.close(fig)

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

    def _signal_changes(self, signals_df: pd.DataFrame) -> int | None:
        if signals_df.empty or "target_signal" not in signals_df:
            return None
        target = pd.to_numeric(signals_df["target_signal"], errors="coerce").fillna(0.0)
        return int(target.diff().abs().fillna(0.0).gt(1e-9).sum())

    def _exposure_mean(self, signals_df: pd.DataFrame) -> float | None:
        if signals_df.empty or "target_signal" not in signals_df:
            return None
        return float(pd.to_numeric(signals_df["target_signal"], errors="coerce").fillna(0.0).mean())

    def _trade_summary(self, orders_df: pd.DataFrame) -> dict[str, Any]:
        if orders_df.empty:
            return {
                "trade_count": 0,
                "buy_count": 0,
                "sell_count": 0,
                "first_trade_date": None,
                "last_trade_date": None,
                "net_size": 0.0,
                "gross_turnover": 0.0,
                "avg_trade_value": 0.0,
            }
        values = pd.to_numeric(orders_df.get("value", pd.Series(dtype=float)), errors="coerce").abs().fillna(0.0)
        sizes = pd.to_numeric(orders_df.get("size", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        is_buy = orders_df.get("is_buy", pd.Series([False] * len(orders_df))).astype(bool)
        datetimes = pd.to_datetime(orders_df["datetime"])
        return {
            "trade_count": int(len(orders_df)),
            "buy_count": int(is_buy.sum()),
            "sell_count": int((~is_buy).sum()),
            "first_trade_date": str(datetimes.min().date()),
            "last_trade_date": str(datetimes.max().date()),
            "net_size": float(sizes.sum()),
            "gross_turnover": float(values.sum()),
            "avg_trade_value": float(values.mean()) if len(values) else 0.0,
        }

    def _trade_rows(self, *, segment_orders: pd.DataFrame, signals_df: pd.DataFrame) -> list[dict[str, Any]]:
        if segment_orders.empty:
            return []
        ordered = segment_orders.sort_values("datetime").copy()
        rows: list[dict[str, Any]] = []
        for _, order in ordered.iterrows():
            dt = pd.to_datetime(order["datetime"])
            before, after = self._signal_context(signals_df=signals_df, dt=dt)
            rows.append(
                {
                    "datetime": str(dt.date()),
                    "side": self._trade_side(order),
                    "size": self._safe_float(order.get("size")),
                    "price": self._safe_float(order.get("price")),
                    "value": self._safe_float(order.get("value")),
                    "commission": self._safe_float(order.get("commission")),
                    "target_signal_before": before,
                    "target_signal_after": after,
                }
            )
        return rows

    def _signal_context(self, *, signals_df: pd.DataFrame, dt: pd.Timestamp) -> tuple[float | None, float | None]:
        if signals_df.empty or "datetime" not in signals_df or "target_signal" not in signals_df:
            return None, None
        signals = signals_df.sort_values("datetime")
        before_rows = signals[signals["datetime"] < dt]
        after_rows = signals[signals["datetime"] <= dt]
        before = None if before_rows.empty else self._safe_float(before_rows.iloc[-1].get("target_signal"))
        after = None if after_rows.empty else self._safe_float(after_rows.iloc[-1].get("target_signal"))
        return before, after

    def _trade_side(self, order: pd.Series) -> str:
        if "is_buy" in order:
            return "buy" if bool(order.get("is_buy")) else "sell"
        size = self._safe_float(order.get("size")) or 0.0
        return "buy" if size > 0 else "sell"

    def _market_profile_path(self, state: dict[str, Any]) -> Path:
        profile = state.get("artifacts", {}).get("market_profile", {})
        primary = profile.get("primary", {}) if isinstance(profile.get("primary"), dict) else {}
        raw_path = profile.get("profile_path") or primary.get("json_path") or state.get("steps", {}).get("market_profile", {}).get("profile_path")
        if not raw_path:
            raise FileNotFoundError("run_state.json 中没有市场画像 JSON 路径。")
        path = self._resolve_path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"市场画像文件不存在：{path}")
        return path

    def _resolve_output_dir(self, output_dir: Path | None, attempt: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        return self._resolve_path(attempt["attempt_dir"] if "attempt_dir" in attempt else attempt["review_dir"]).parent / "analysis" / "stage_attribution"

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
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    def _fmt(self, value: Any) -> str:
        if value is None:
            return ""
        try:
            return f"{float(value):.4%}"
        except (TypeError, ValueError):
            return str(value)

    def _fmt_number(self, value: Any) -> str:
        if value is None:
            return ""
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return str(value)

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
