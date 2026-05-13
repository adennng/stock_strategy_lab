from __future__ import annotations

import importlib
import importlib.util
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.config.loader import load_config_file
from strategy_lab.signals.base import SignalStrategyProtocol, clamp_signal

try:
    import backtrader as bt
except Exception:  # pragma: no cover - optional dependency check happens in run()
    bt = None


DEFAULT_STRATEGY = "strategy_lab.signals.baselines.ma20_signal:Strategy"


class SignalBacktestRequest(BaseModel):
    data_path: Path
    output_dir: Path | None = None
    run_state_path: Path | None = None
    strategy: str = DEFAULT_STRATEGY
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    initial_cash: float | None = None
    commission: float | None = None
    slippage_perc: float | None = None
    allow_short: bool | None = None
    run_id: str | None = None
    quantstats_html: bool = False
    evaluation_start: str | None = None
    evaluation_end: str | None = None


class SignalBacktestResult(BaseModel):
    run_id: str
    output_dir: Path
    data_path: Path
    strategy: str
    metrics_path: Path
    report_path: Path
    signals_path: Path
    equity_curve_path: Path
    benchmark_curve_path: Path
    orders_path: Path
    comparison_chart_path: Path | None = None
    quantstats_report_path: Path | None = None
    metrics: dict[str, Any]


@dataclass
class _BacktestRecords:
    signals: list[dict[str, Any]]
    orders: list[dict[str, Any]]


class SignalBacktestEvaluator:
    """信号层回测评估器。

    策略输出目标预算使用比例 S，Backtrader 按 target percent 下单。
    默认不启用 cheat-on-close，因此当前 K 线计算出的信号在下一根 K 线撮合，避免直接用未来价格成交。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()

    def run(self, request: SignalBacktestRequest) -> SignalBacktestResult:
        if bt is None:
            raise RuntimeError("缺少 backtrader，请先安装：python -m pip install backtrader")

        run_state = self._load_run_state(request.run_state_path)
        settings = self._resolve_settings(request, run_state=run_state)
        data_path = self._resolve_path(request.data_path)
        df = self._load_ohlcv(data_path)
        evaluation_start, evaluation_end = self._resolve_evaluation_window(
            df=df,
            evaluation_start=request.evaluation_start,
            evaluation_end=request.evaluation_end,
        )
        evaluation_df = self._filter_evaluation_frame(
            df=df,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )
        strategy_cls = self._load_strategy_class(request.strategy)
        self._validate_strategy_class(strategy_cls)

        run_id = request.run_id or self._new_run_id(df, request.strategy)
        output_dir = self._resolve_output_dir(request.output_dir, run_id, run_state=run_state)
        output_dir.mkdir(parents=True, exist_ok=True)

        records = _BacktestRecords(signals=[], orders=[])
        feed_df = self._to_backtrader_frame(df)

        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.setcash(settings["initial_cash"])
        cerebro.broker.setcommission(commission=settings["commission"])
        cerebro.broker.set_slippage_perc(
            perc=settings["slippage_perc"],
            slip_open=True,
            slip_limit=True,
            slip_match=True,
            slip_out=False,
        )
        data_feed = bt.feeds.PandasData(dataname=feed_df)
        cerebro.adddata(data_feed)
        cerebro.addstrategy(
            _SignalBacktraderStrategy,
            signal_strategy_cls=strategy_cls,
            signal_params=request.strategy_params,
            source_df=df,
            records=records,
            allow_short=settings["allow_short"],
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )
        cerebro.run()

        signals_df = pd.DataFrame(records.signals)
        orders_df = pd.DataFrame(records.orders)
        equity_df = self._build_equity_curve(signals_df)
        benchmark_df = self._build_buy_hold_benchmark(df=evaluation_df, initial_cash=settings["initial_cash"])
        equity_df = self._attach_benchmark_to_equity(equity_df=equity_df, benchmark_df=benchmark_df)
        metrics = self._compute_metrics(
            equity_df=equity_df,
            signals_df=signals_df,
            orders_df=orders_df,
            initial_cash=settings["initial_cash"],
        )
        metrics.update(
            {
                "run_id": run_id,
                "strategy": request.strategy,
                "strategy_params": request.strategy_params,
                "data_path": str(data_path),
                "run_state_path": str(self._resolve_path(request.run_state_path)) if request.run_state_path else None,
                "start_date": str(evaluation_df["datetime"].min().date()),
                "end_date": str(evaluation_df["datetime"].max().date()),
                "bar_count": int(len(evaluation_df)),
                "context_start_date": str(df["datetime"].min().date()),
                "context_end_date": str(df["datetime"].max().date()),
                "context_bar_count": int(len(df)),
                "evaluation_start": str(evaluation_start.date()),
                "evaluation_end": str(evaluation_end.date()),
                "settings": settings,
            }
        )

        signals_path = output_dir / "daily_signals.parquet"
        equity_curve_path = output_dir / "equity_curve.parquet"
        benchmark_curve_path = output_dir / "benchmark_curve.parquet"
        orders_path = output_dir / "orders.parquet"
        metrics_path = output_dir / "metrics.json"
        report_path = output_dir / "report.md"
        config_path = output_dir / "backtest_request.json"
        comparison_chart_path = output_dir / "strategy_vs_benchmark.png"

        signals_df.to_parquet(signals_path, index=False)
        equity_df.to_parquet(equity_curve_path, index=False)
        benchmark_df.to_parquet(benchmark_curve_path, index=False)
        orders_df.to_parquet(orders_path, index=False)
        self._write_strategy_vs_benchmark_chart(equity_df=equity_df, output_path=comparison_chart_path)
        metrics["benchmark_curve_path"] = str(benchmark_curve_path)
        metrics["comparison_chart_path"] = str(comparison_chart_path)
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        config_path.write_text(
            request.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
        report_path.write_text(
            self._format_report(metrics=metrics, output_dir=output_dir),
            encoding="utf-8",
        )
        quantstats_report_path = output_dir / "quantstats_report.html" if request.quantstats_html else None
        if quantstats_report_path:
            html_warning_path = self._try_write_quantstats_html(equity_df, quantstats_report_path)
            if quantstats_report_path.exists():
                metrics["quantstats_report_path"] = str(quantstats_report_path)
            elif html_warning_path:
                metrics["quantstats_html_warning_path"] = str(html_warning_path)
            metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        return SignalBacktestResult(
            run_id=run_id,
            output_dir=output_dir,
            data_path=data_path,
            strategy=request.strategy,
            metrics_path=metrics_path,
            report_path=report_path,
            signals_path=signals_path,
            equity_curve_path=equity_curve_path,
            orders_path=orders_path,
            benchmark_curve_path=benchmark_curve_path,
            comparison_chart_path=comparison_chart_path if comparison_chart_path.exists() else None,
            quantstats_report_path=quantstats_report_path if quantstats_report_path and quantstats_report_path.exists() else None,
            metrics=metrics,
        )

    def generate_quantstats_html(
        self,
        backtest_dir: str | Path,
        output_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """从已有回测目录补生成 QuantStats HTML 报告。"""
        actual_backtest_dir = self._resolve_path(Path(backtest_dir))
        if not actual_backtest_dir.exists():
            raise FileNotFoundError(f"回测目录不存在：{actual_backtest_dir}")
        if not actual_backtest_dir.is_dir():
            raise NotADirectoryError(f"不是回测目录：{actual_backtest_dir}")

        equity_curve_path = actual_backtest_dir / "equity_curve.parquet"
        if not equity_curve_path.exists():
            raise FileNotFoundError(f"回测目录缺少 equity_curve.parquet：{equity_curve_path}")

        equity_df = pd.read_parquet(equity_curve_path)
        if "datetime" not in equity_df.columns:
            raise ValueError("equity_curve.parquet 缺少 datetime 字段。")
        if "return" not in equity_df.columns:
            if "portfolio_value" not in equity_df.columns:
                raise ValueError("equity_curve.parquet 缺少 return 字段，且无法从 portfolio_value 重新计算。")
            equity_df = equity_df.copy()
            equity_df["return"] = pd.to_numeric(equity_df["portfolio_value"], errors="coerce").pct_change().fillna(0.0)

        actual_output_path = self._resolve_path(Path(output_path)) if output_path else actual_backtest_dir / "quantstats_report.html"
        warning_path = self._try_write_quantstats_html(equity_df, actual_output_path)
        metrics_path = actual_backtest_dir / "metrics.json"
        metrics = self._load_metrics_for_update(metrics_path)

        result = {
            "backtest_dir": str(actual_backtest_dir),
            "equity_curve_path": str(equity_curve_path),
            "quantstats_report_path": str(actual_output_path) if actual_output_path.exists() else None,
            "warning_path": str(warning_path) if warning_path else None,
            "metrics_path": str(metrics_path) if metrics_path.exists() else None,
        }
        if metrics_path.exists():
            if actual_output_path.exists():
                metrics["quantstats_report_path"] = str(actual_output_path)
                metrics.pop("quantstats_html_warning_path", None)
            elif warning_path:
                metrics["quantstats_html_warning_path"] = str(warning_path)
            metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return result

    def _resolve_settings(self, request: SignalBacktestRequest, run_state: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = load_config_file("backtest").get("backtest", {})
        run_cfg = (run_state or {}).get("backtest_config", {})
        merged = {**cfg, **run_cfg}
        return {
            "initial_cash": float(request.initial_cash if request.initial_cash is not None else merged.get("initial_cash", 1_000_000)),
            "commission": float(request.commission if request.commission is not None else merged.get("commission", 0.0)),
            "slippage_perc": float(request.slippage_perc if request.slippage_perc is not None else merged.get("slippage_perc", 0.0001)),
            "allow_short": bool(request.allow_short if request.allow_short is not None else merged.get("allow_short", False)),
        }

    def _resolve_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        return self.config.root_dir / path

    def _resolve_output_dir(self, output_dir: Path | None, run_id: str, run_state: dict[str, Any] | None = None) -> Path:
        if output_dir is not None:
            return self._resolve_path(output_dir)
        if run_state:
            backtests_dir = run_state.get("directories", {}).get("backtests")
            if backtests_dir:
                return self._resolve_path(Path(backtests_dir) / run_id)
        return self.config.root_dir / "artifacts" / "signal_backtests" / run_id

    def _load_run_state(self, run_state_path: Path | None) -> dict[str, Any] | None:
        if not run_state_path:
            return None
        path = self._resolve_path(run_state_path)
        if not path.exists():
            raise FileNotFoundError(f"run_state.json 不存在：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_evaluation_window(
        self,
        *,
        df: pd.DataFrame,
        evaluation_start: str | None,
        evaluation_end: str | None,
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        data_start = pd.to_datetime(df["datetime"].min())
        data_end = pd.to_datetime(df["datetime"].max())
        start = pd.to_datetime(evaluation_start) if evaluation_start else data_start
        end = pd.to_datetime(evaluation_end) if evaluation_end else data_end
        if pd.isna(start) or pd.isna(end):
            raise ValueError("evaluation_start/evaluation_end 日期无法解析。")
        if start > end:
            raise ValueError("evaluation_start 不能晚于 evaluation_end。")
        if end < data_start or start > data_end:
            raise ValueError("评估区间与数据区间没有交集。")
        return max(start, data_start), min(end, data_end)

    def _filter_evaluation_frame(
        self,
        *,
        df: pd.DataFrame,
        evaluation_start: pd.Timestamp,
        evaluation_end: pd.Timestamp,
    ) -> pd.DataFrame:
        mask = (pd.to_datetime(df["datetime"]) >= evaluation_start) & (pd.to_datetime(df["datetime"]) <= evaluation_end)
        evaluation_df = df.loc[mask].copy().reset_index(drop=True)
        if evaluation_df.empty:
            raise ValueError("评估区间内没有可用数据。")
        return evaluation_df

    def _new_run_id(self, df: pd.DataFrame, strategy: str) -> str:
        symbol = "asset"
        if "symbol" in df and not df["symbol"].dropna().empty:
            symbol = str(df["symbol"].dropna().iloc[0]).replace(".", "_")
        strategy_name = strategy.split(":")[-1].split(".")[-1].lower()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"signal_bt_{symbol}_{strategy_name}_{timestamp}"

    def _load_ohlcv(self, data_path: Path) -> pd.DataFrame:
        if not data_path.exists():
            raise FileNotFoundError(f"数据文件不存在：{data_path}")
        suffix = data_path.suffix.lower()
        if suffix == ".parquet":
            df = pd.read_parquet(data_path)
        elif suffix in {".csv", ".txt"}:
            df = pd.read_csv(data_path)
        elif suffix in {".json", ".jsonl"}:
            df = pd.read_json(data_path, lines=suffix == ".jsonl")
        else:
            raise ValueError(f"暂不支持的数据文件格式：{suffix}")

        required = ["datetime", "open", "high", "low", "close"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise ValueError(f"数据缺少回测必需字段：{missing}")

        normalized = df.copy()
        normalized["datetime"] = self._parse_datetime(normalized["datetime"])
        for column in ["open", "high", "low", "close", "volume"]:
            if column not in normalized:
                normalized[column] = 0
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        if "openinterest" not in normalized:
            normalized["openinterest"] = 0

        normalized = normalized.dropna(subset=["datetime", "open", "high", "low", "close"])
        normalized = normalized.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
        normalized = normalized.reset_index(drop=True)
        if normalized.empty:
            raise ValueError("清洗后的 OHLCV 数据为空。")
        return normalized

    def _parse_datetime(self, series: pd.Series) -> pd.Series:
        if pd.api.types.is_numeric_dtype(series):
            text = series.astype("Int64").astype(str)
            if text.str.len().eq(8).all():
                return pd.to_datetime(text, format="%Y%m%d", errors="coerce")
            return pd.to_datetime(series, unit="ms", errors="coerce")
        text = series.astype(str)
        if text.str.fullmatch(r"\d{8}").all():
            return pd.to_datetime(text, format="%Y%m%d", errors="coerce")
        return pd.to_datetime(series, errors="coerce")

    def _to_backtrader_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        feed_df = df[["datetime", "open", "high", "low", "close", "volume", "openinterest"]].copy()
        feed_df = feed_df.set_index("datetime")
        return feed_df

    def _load_strategy_class(self, strategy: str):
        module_ref, _, class_name = strategy.partition(":")
        if not class_name:
            class_name = "Strategy"

        path = Path(module_ref)
        if path.suffix == ".py" or path.exists():
            actual_path = self._resolve_path(path)
            spec = importlib.util.spec_from_file_location(actual_path.stem, actual_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法加载策略文件：{actual_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        else:
            module = importlib.import_module(module_ref)
        return getattr(module, class_name)

    def _validate_strategy_class(self, strategy_cls: Any) -> None:
        instance = strategy_cls({})
        if not isinstance(instance, SignalStrategyProtocol):
            raise TypeError("策略类必须实现 suggest(history, current_position_in_budget) 方法。")

    def _build_equity_curve(self, signals_df: pd.DataFrame) -> pd.DataFrame:
        if signals_df.empty:
            return pd.DataFrame(columns=["datetime", "portfolio_value", "return"])
        equity = signals_df[["datetime", "portfolio_value"]].copy()
        equity["datetime"] = pd.to_datetime(equity["datetime"])
        equity = equity.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime")
        equity["return"] = equity["portfolio_value"].pct_change().fillna(0.0)
        return equity.reset_index(drop=True)

    def _build_buy_hold_benchmark(self, df: pd.DataFrame, initial_cash: float) -> pd.DataFrame:
        benchmark = df[["datetime", "close"]].copy()
        benchmark["datetime"] = pd.to_datetime(benchmark["datetime"])
        benchmark = benchmark.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime")
        benchmark["benchmark_return"] = benchmark["close"].pct_change().fillna(0.0)
        first_close = float(benchmark["close"].iloc[0])
        if first_close <= 0:
            raise ValueError("首日 close 必须大于 0，无法生成买入持有基准。")
        benchmark["benchmark_value"] = float(initial_cash) * benchmark["close"] / first_close
        benchmark["benchmark_cum_return"] = benchmark["benchmark_value"] / float(initial_cash) - 1.0
        return benchmark[["datetime", "benchmark_value", "benchmark_return", "benchmark_cum_return"]].reset_index(drop=True)

    def _attach_benchmark_to_equity(self, equity_df: pd.DataFrame, benchmark_df: pd.DataFrame) -> pd.DataFrame:
        if equity_df.empty:
            return equity_df
        merged = equity_df.merge(benchmark_df, on="datetime", how="left")
        merged[["benchmark_value", "benchmark_return", "benchmark_cum_return"]] = merged[
            ["benchmark_value", "benchmark_return", "benchmark_cum_return"]
        ].ffill()
        merged["strategy_cum_return"] = merged["portfolio_value"] / merged["portfolio_value"].iloc[0] - 1.0
        merged["excess_return"] = merged["return"] - merged["benchmark_return"]
        merged["excess_cum_return"] = (1.0 + merged["return"]).cumprod() / (1.0 + merged["benchmark_return"]).cumprod() - 1.0
        return merged

    def _compute_metrics(
        self,
        equity_df: pd.DataFrame,
        signals_df: pd.DataFrame,
        orders_df: pd.DataFrame,
        initial_cash: float,
    ) -> dict[str, Any]:
        if equity_df.empty:
            return {"error": "empty_equity_curve"}

        values = pd.to_numeric(equity_df["portfolio_value"], errors="coerce")
        returns = pd.to_numeric(equity_df["return"], errors="coerce").fillna(0.0)
        final_value = float(values.iloc[-1])
        total_return = final_value / initial_cash - 1.0
        periods = max(int(len(equity_df)), 1)
        annual_return = (1.0 + total_return) ** (252.0 / periods) - 1.0 if total_return > -1 else -1.0
        annual_volatility = float(returns.std(ddof=0) * math.sqrt(252.0))
        sharpe = float(returns.mean() / returns.std(ddof=0) * math.sqrt(252.0)) if returns.std(ddof=0) > 0 else 0.0
        drawdown = values / values.cummax() - 1.0
        max_drawdown = float(drawdown.min())
        calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else None
        win_rate = float((returns > 0).sum() / max((returns != 0).sum(), 1))

        exposure_mean = None
        signal_changes = None
        if not signals_df.empty and "target_signal" in signals_df:
            target = pd.to_numeric(signals_df["target_signal"], errors="coerce").fillna(0.0)
            exposure_mean = float(target.mean())
            signal_changes = int(target.diff().abs().fillna(0.0).gt(1e-9).sum())

        metrics = {
            "initial_cash": initial_cash,
            "final_value": final_value,
            "total_return": total_return,
            "annual_return": annual_return,
            "annual_volatility": annual_volatility,
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "calmar": calmar,
            "win_rate": win_rate,
            "exposure_mean": exposure_mean,
            "signal_changes": signal_changes,
            "order_count": int(len(orders_df)),
        }
        metrics.update(self._benchmark_metrics(equity_df=equity_df, initial_cash=initial_cash))
        metrics.update(self._quantstats_metrics(equity_df))
        return metrics

    def _benchmark_metrics(self, equity_df: pd.DataFrame, initial_cash: float) -> dict[str, Any]:
        required = {"benchmark_value", "benchmark_return", "excess_return"}
        if not required.issubset(set(equity_df.columns)):
            return {"benchmark_warning": "equity_curve_missing_benchmark_columns"}

        benchmark_values = pd.to_numeric(equity_df["benchmark_value"], errors="coerce")
        benchmark_returns = pd.to_numeric(equity_df["benchmark_return"], errors="coerce").fillna(0.0)
        strategy_returns = pd.to_numeric(equity_df["return"], errors="coerce").fillna(0.0)
        excess_returns = pd.to_numeric(equity_df["excess_return"], errors="coerce").fillna(0.0)
        periods = max(int(len(equity_df)), 1)

        benchmark_final_value = float(benchmark_values.iloc[-1])
        benchmark_total_return = benchmark_final_value / initial_cash - 1.0
        benchmark_annual_return = (1.0 + benchmark_total_return) ** (252.0 / periods) - 1.0 if benchmark_total_return > -1 else -1.0
        benchmark_annual_volatility = float(benchmark_returns.std(ddof=0) * math.sqrt(252.0))
        benchmark_sharpe = (
            float(benchmark_returns.mean() / benchmark_returns.std(ddof=0) * math.sqrt(252.0))
            if benchmark_returns.std(ddof=0) > 0
            else 0.0
        )
        benchmark_drawdown = benchmark_values / benchmark_values.cummax() - 1.0
        benchmark_max_drawdown = float(benchmark_drawdown.min())
        tracking_error = float(excess_returns.std(ddof=0) * math.sqrt(252.0))
        information_ratio = (
            float(excess_returns.mean() / excess_returns.std(ddof=0) * math.sqrt(252.0))
            if excess_returns.std(ddof=0) > 0
            else None
        )
        correlation = strategy_returns.corr(benchmark_returns)

        strategy_total_return = float(equity_df["portfolio_value"].iloc[-1]) / initial_cash - 1.0
        return {
            "benchmark_name": "buy_and_hold",
            "benchmark_final_value": benchmark_final_value,
            "benchmark_total_return": benchmark_total_return,
            "benchmark_annual_return": benchmark_annual_return,
            "benchmark_annual_volatility": benchmark_annual_volatility,
            "benchmark_sharpe": benchmark_sharpe,
            "benchmark_max_drawdown": benchmark_max_drawdown,
            "excess_total_return": strategy_total_return - benchmark_total_return,
            "tracking_error": tracking_error,
            "information_ratio": information_ratio,
            "correlation_to_benchmark": self._safe_metric_value(correlation),
            "outperform_day_ratio": float((strategy_returns > benchmark_returns).mean()),
        }

    def _quantstats_metrics(self, equity_df: pd.DataFrame) -> dict[str, Any]:
        try:
            import quantstats as qs

            clean = equity_df.set_index("datetime")["return"].astype(float)
            clean.index = pd.to_datetime(clean.index)
            stats_map = {
                "qs_cagr": qs.stats.cagr,
                "qs_sharpe": qs.stats.sharpe,
                "qs_sortino": qs.stats.sortino,
                "qs_calmar": qs.stats.calmar,
                "qs_max_drawdown": qs.stats.max_drawdown,
                "qs_volatility": qs.stats.volatility,
                "qs_omega": qs.stats.omega,
                "qs_skew": qs.stats.skew,
                "qs_kurtosis": qs.stats.kurtosis,
                "qs_tail_ratio": qs.stats.tail_ratio,
                "qs_gain_to_pain_ratio": qs.stats.gain_to_pain_ratio,
                "qs_profit_factor": qs.stats.profit_factor,
                "qs_payoff_ratio": qs.stats.payoff_ratio,
                "qs_win_rate": qs.stats.win_rate,
                "qs_best": qs.stats.best,
                "qs_worst": qs.stats.worst,
                "qs_avg_return": qs.stats.avg_return,
                "qs_avg_win": qs.stats.avg_win,
                "qs_avg_loss": qs.stats.avg_loss,
                "qs_expected_return": qs.stats.expected_return,
            }
            return {name: self._safe_metric_value(func(clean)) for name, func in stats_map.items()}
        except Exception as exc:
            return {"quantstats_warning": str(exc)}

    def _safe_metric_value(self, value: Any) -> float | None:
        if hasattr(value, "item"):
            value = value.item()
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    def _load_metrics_for_update(self, metrics_path: Path) -> dict[str, Any]:
        if not metrics_path.exists():
            return {}
        try:
            return json.loads(metrics_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _format_report(self, metrics: dict[str, Any], output_dir: Path) -> str:
        percent_keys = {
            "total_return",
            "annual_return",
            "annual_volatility",
            "max_drawdown",
            "win_rate",
            "benchmark_total_return",
            "benchmark_annual_return",
            "benchmark_annual_volatility",
            "benchmark_max_drawdown",
            "excess_total_return",
            "tracking_error",
            "outperform_day_ratio",
        }
        lines = [
            "# Signal Backtest Report",
            "",
            f"- Run ID: {metrics.get('run_id')}",
            f"- Strategy: {metrics.get('strategy')}",
            f"- Data: {metrics.get('data_path')}",
            f"- Period: {metrics.get('start_date')} to {metrics.get('end_date')}",
            f"- Output: {output_dir}",
            "",
            "## Metrics",
        ]
        strategy_keys = [
            "initial_cash",
            "final_value",
            "total_return",
            "annual_return",
            "annual_volatility",
            "sharpe",
            "max_drawdown",
            "calmar",
            "win_rate",
            "exposure_mean",
            "signal_changes",
            "order_count",
        ]
        for key in strategy_keys:
            value = metrics.get(key)
            lines.append(f"- {key}: {self._render_report_value(key, value, percent_keys)}")

        lines.extend(["", "## Benchmark"])
        for key in [
            "benchmark_name",
            "benchmark_final_value",
            "benchmark_total_return",
            "benchmark_annual_return",
            "benchmark_annual_volatility",
            "benchmark_sharpe",
            "benchmark_max_drawdown",
            "excess_total_return",
            "tracking_error",
            "information_ratio",
            "correlation_to_benchmark",
            "outperform_day_ratio",
        ]:
            value = metrics.get(key)
            lines.append(f"- {key}: {self._render_report_value(key, value, percent_keys)}")
        lines.extend(
            [
                "",
                "## Artifacts",
                "- daily_signals.parquet",
                "- equity_curve.parquet",
                "- benchmark_curve.parquet",
                "- orders.parquet",
                "- metrics.json",
                "- backtest_request.json",
                "- strategy_vs_benchmark.png",
                "- quantstats_report.html (when enabled)",
                "",
            ]
        )
        return "\n".join(lines)

    def _render_report_value(self, key: str, value: Any, percent_keys: set[str]) -> str:
        if value is None:
            return "null"
        if key in percent_keys:
            return f"{float(value):.2%}"
        if isinstance(value, float):
            return f"{value:.6f}"
        return str(value)

    def _write_strategy_vs_benchmark_chart(self, equity_df: pd.DataFrame, output_path: Path) -> None:
        try:
            import matplotlib

            matplotlib.use("Agg", force=True)
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise RuntimeError("生成策略与基准对比图需要 matplotlib。") from exc

        if equity_df.empty or "benchmark_value" not in equity_df.columns:
            return

        chart_df = equity_df.copy()
        chart_df["datetime"] = pd.to_datetime(chart_df["datetime"])
        strategy_value = pd.to_numeric(chart_df["portfolio_value"], errors="coerce")
        benchmark_value = pd.to_numeric(chart_df["benchmark_value"], errors="coerce")
        strategy_norm = strategy_value / strategy_value.iloc[0]
        benchmark_norm = benchmark_value / benchmark_value.iloc[0]
        strategy_dd = strategy_value / strategy_value.cummax() - 1.0
        benchmark_dd = benchmark_value / benchmark_value.cummax() - 1.0

        fig, (ax_equity, ax_dd) = plt.subplots(
            2,
            1,
            figsize=(13, 8),
            sharex=True,
            gridspec_kw={"height_ratios": [3.0, 1.3]},
        )
        dates = chart_df["datetime"]
        ax_equity.plot(dates, strategy_norm, label="Strategy", color="#2563eb", linewidth=1.8)
        ax_equity.plot(dates, benchmark_norm, label="Buy & Hold", color="#f97316", linewidth=1.5)
        ax_equity.set_title("Strategy vs Buy & Hold Benchmark", loc="left", fontsize=13, fontweight="bold")
        ax_equity.set_ylabel("Growth of 1.0")
        ax_equity.grid(True, alpha=0.25)
        ax_equity.legend(loc="upper left", frameon=False)

        ax_dd.plot(dates, strategy_dd, label="Strategy DD", color="#1d4ed8", linewidth=1.1)
        ax_dd.plot(dates, benchmark_dd, label="Buy & Hold DD", color="#ea580c", linewidth=1.1)
        ax_dd.fill_between(dates, strategy_dd, 0, color="#2563eb", alpha=0.15)
        ax_dd.fill_between(dates, benchmark_dd, 0, color="#f97316", alpha=0.12)
        ax_dd.set_ylabel("Drawdown")
        ax_dd.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
        ax_dd.grid(True, alpha=0.25)
        ax_dd.legend(loc="lower left", frameon=False, ncols=2)

        locator = mdates.AutoDateLocator(minticks=5, maxticks=10)
        ax_dd.xaxis.set_major_locator(locator)
        ax_dd.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        fig.tight_layout()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=160, bbox_inches="tight")
        plt.close(fig)

    def _try_write_quantstats_html(self, equity_df: pd.DataFrame, output_path: Path) -> Path | None:
        try:
            import matplotlib

            matplotlib.use("Agg", force=True)
            import quantstats as qs

            output_path.parent.mkdir(parents=True, exist_ok=True)
            warning_path = output_path.with_suffix(".warning.txt")
            if warning_path.exists():
                warning_path.unlink()
            returns = equity_df.set_index("datetime")["return"].astype(float)
            benchmark = None
            if "benchmark_return" in equity_df.columns:
                benchmark = equity_df.set_index("datetime")["benchmark_return"].astype(float)
            if benchmark is not None:
                qs.reports.html(returns, benchmark=benchmark, output=str(output_path), title="Signal Backtest")
            else:
                qs.reports.html(returns, output=str(output_path), title="Signal Backtest")
            return None
        except Exception as exc:
            warning_path = output_path.with_suffix(".warning.txt")
            warning_path.write_text(str(exc), encoding="utf-8")
            return warning_path


class _SignalBacktraderStrategy(bt.Strategy if bt is not None else object):
    params = (
        ("signal_strategy_cls", None),
        ("signal_params", None),
        ("source_df", None),
        ("records", None),
        ("allow_short", False),
        ("evaluation_start", None),
        ("evaluation_end", None),
    )

    def __init__(self):
        self.signal_strategy = self.p.signal_strategy_cls(self.p.signal_params or {})
        self.source_df = self.p.source_df
        self.records = self.p.records
        self.allow_short = bool(self.p.allow_short)
        self.evaluation_start = pd.to_datetime(self.p.evaluation_start) if self.p.evaluation_start is not None else None
        self.evaluation_end = pd.to_datetime(self.p.evaluation_end) if self.p.evaluation_end is not None else None

    def next(self):
        length = len(self.data)
        history = self.source_df.iloc[:length].copy()
        dt = pd.to_datetime(self.data.datetime.datetime(0))
        if self.evaluation_start is not None and dt < self.evaluation_start:
            return
        if self.evaluation_end is not None and dt > self.evaluation_end:
            return
        value = float(self.broker.getvalue())
        close = float(self.data.close[0])
        position_value = float(self.position.size) * close
        current_exposure = position_value / value if value else 0.0
        raw_signal = self.signal_strategy.suggest(
            history=history,
            current_position_in_budget=current_exposure,
        )
        target_signal = clamp_signal(raw_signal, allow_short=self.allow_short)
        self.records.signals.append(
            {
                "datetime": dt,
                "close": close,
                "raw_signal": float(raw_signal),
                "target_signal": target_signal,
                "current_exposure": current_exposure,
                "portfolio_value": value,
                "cash": float(self.broker.getcash()),
                "position_size": float(self.position.size),
            }
        )
        self.order_target_percent(target=target_signal)

    def notify_order(self, order):
        if order.status != order.Completed:
            return
        dt = pd.to_datetime(self.data.datetime.datetime(0))
        if self.evaluation_start is not None and dt < self.evaluation_start:
            return
        if self.evaluation_end is not None and dt > self.evaluation_end:
            return
        self.records.orders.append(
            {
                "datetime": dt,
                "is_buy": bool(order.isbuy()),
                "size": float(order.executed.size),
                "price": float(order.executed.price),
                "value": float(order.executed.value),
                "commission": float(order.executed.comm),
            }
        )
