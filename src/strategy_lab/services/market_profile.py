from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel

from strategy_lab.config import AppConfig, load_app_config


class MarketProfileRequest(BaseModel):
    data_path: Path | None = None
    run_state_path: Path | None = None
    output_dir: Path | None = None
    profile_id: str = "market_profile"
    smooth_window: int = 5
    min_segment_days: int = 10
    output_format: str = "both"
    generate_chart: bool = True


class MarketProfileResult(BaseModel):
    profile_id: str
    data_path: Path
    output_dir: Path
    json_path: Path | None = None
    markdown_path: Path | None = None
    chart_path: Path | None = None
    run_state_path: Path | None = None
    summary: dict[str, Any]


class MarketProfileService:
    """信号层市场画像服务。

    只做确定性计算和事实型描述，不调用 LLM，不输出策略建议。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()

    def run(self, request: MarketProfileRequest) -> MarketProfileResult:
        run_state = self._load_run_state(request.run_state_path)
        data_path = self._resolve_data_path(request.data_path, run_state=run_state)
        df = self._load_ohlcv(data_path)
        profile = self._build_profile(
            df=df,
            data_path=data_path,
            smooth_window=request.smooth_window,
            min_segment_days=request.min_segment_days,
        )
        output_dir = self._resolve_output_dir(request.output_dir, request.profile_id, run_state=run_state)
        output_dir.mkdir(parents=True, exist_ok=True)

        output_format = request.output_format.lower()
        if output_format not in {"json", "md", "both"}:
            raise ValueError("output_format 必须是 json、md 或 both。")

        json_path = output_dir / f"{request.profile_id}.json" if output_format in {"json", "both"} else None
        markdown_path = output_dir / f"{request.profile_id}.md" if output_format in {"md", "both"} else None
        chart_path = output_dir / f"{request.profile_id}_chart.png" if request.generate_chart else None
        if chart_path:
            self._write_chart(df=df, profile=profile, chart_path=chart_path)
            profile["visualizations"] = {
                "price_regime_chart": str(chart_path),
                "price_regime_chart_filename": chart_path.name,
            }
        if json_path:
            json_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if markdown_path:
            markdown_path.write_text(self._format_markdown(profile), encoding="utf-8")

        if request.run_state_path:
            self._update_run_state(
                request.run_state_path,
                profile=profile,
                json_path=json_path,
                markdown_path=markdown_path,
                chart_path=chart_path,
            )

        return MarketProfileResult(
            profile_id=request.profile_id,
            data_path=data_path,
            output_dir=output_dir,
            json_path=json_path,
            markdown_path=markdown_path,
            chart_path=chart_path,
            run_state_path=self._resolve_path(request.run_state_path) if request.run_state_path else None,
            summary=profile["summary"],
        )

    def _build_profile(self, df: pd.DataFrame, data_path: Path, smooth_window: int, min_segment_days: int) -> dict[str, Any]:
        returns = df["close"].pct_change().fillna(0.0)
        wealth = (1.0 + returns).cumprod()
        drawdown = wealth / wealth.cummax() - 1.0

        summary = self._summary(df=df, data_path=data_path)
        return_profile = self._return_profile(df=df, returns=returns)
        drawdown_profile = self._drawdown_profile(df=df, wealth=wealth, drawdown=drawdown)
        trend_profile = self._trend_profile(df=df)
        volatility_profile = self._volatility_profile(df=df, returns=returns)
        regime_segments = self._regime_segments(
            df=df,
            smooth_window=smooth_window,
            min_segment_days=min_segment_days,
        )
        liquidity_profile = self._liquidity_profile(df=df)
        risk_events = self._risk_events(df=df, returns=returns, drawdown_profile=drawdown_profile)
        profile_flags = self._profile_flags(
            return_profile=return_profile,
            drawdown_profile=drawdown_profile,
            trend_profile=trend_profile,
            volatility_profile=volatility_profile,
        )
        fact_descriptions = self._fact_descriptions(
            summary=summary,
            return_profile=return_profile,
            drawdown_profile=drawdown_profile,
            trend_profile=trend_profile,
            volatility_profile=volatility_profile,
            regime_segments=regime_segments,
        )

        return {
            "created_at": datetime.now().isoformat(),
            "summary": summary,
            "return_profile": return_profile,
            "drawdown_profile": drawdown_profile,
            "trend_profile": trend_profile,
            "volatility_profile": volatility_profile,
            "regime_segments": regime_segments,
            "liquidity_profile": liquidity_profile,
            "profile_flags": profile_flags,
            "risk_events": risk_events,
            "fact_descriptions": fact_descriptions,
            "methodology": {
                "description": "确定性规则画像；不调用 LLM；文字只描述数据事实，不提供策略建议。",
                "regime_segmentation": {
                    "method": "adaptive_rule_based",
                    "description": "按每日走势状态自适应划分阶段；先根据20/60日收益、均线斜率、价格相对均线和20日波动率打标签，再做短窗口多数平滑，最后合并过短阶段。",
                    "smooth_window": smooth_window,
                    "min_segment_days": min_segment_days,
                },
                "annualization_days": 252,
            },
        }

    def _summary(self, df: pd.DataFrame, data_path: Path) -> dict[str, Any]:
        symbol = None
        if "symbol" in df.columns and not df["symbol"].dropna().empty:
            symbol = str(df["symbol"].dropna().iloc[0])
        total_missing = int(df.isna().sum().sum())
        duplicate_count = int(df.duplicated(subset=["datetime"]).sum())
        price_start = float(df["close"].iloc[0])
        price_end = float(df["close"].iloc[-1])
        return {
            "symbol": symbol,
            "data_path": str(data_path),
            "start_date": str(df["datetime"].min().date()),
            "end_date": str(df["datetime"].max().date()),
            "row_count": int(len(df)),
            "columns": list(df.columns),
            "frequency": "unknown",
            "missing_count": total_missing,
            "duplicate_count": duplicate_count,
            "price_start": price_start,
            "price_end": price_end,
            "total_return": self._safe_float(price_end / price_start - 1.0),
        }

    def _return_profile(self, df: pd.DataFrame, returns: pd.Series) -> dict[str, Any]:
        nonzero_returns = returns.iloc[1:]
        total_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0)
        periods = max(len(df), 1)
        annual_return = (1.0 + total_return) ** (252.0 / periods) - 1.0 if total_return > -1 else -1.0
        annual_volatility = float(nonzero_returns.std(ddof=0) * math.sqrt(252.0))
        sharpe_like = float(nonzero_returns.mean() / nonzero_returns.std(ddof=0) * math.sqrt(252.0)) if nonzero_returns.std(ddof=0) > 0 else 0.0
        best_idx = nonzero_returns.idxmax() if not nonzero_returns.empty else df.index[0]
        worst_idx = nonzero_returns.idxmin() if not nonzero_returns.empty else df.index[0]
        return {
            "daily_return_mean": self._safe_float(nonzero_returns.mean()),
            "daily_return_std": self._safe_float(nonzero_returns.std(ddof=0)),
            "annualized_return": self._safe_float(annual_return),
            "annualized_volatility": self._safe_float(annual_volatility),
            "sharpe_like": self._safe_float(sharpe_like),
            "skew": self._safe_float(nonzero_returns.skew()),
            "kurtosis": self._safe_float(nonzero_returns.kurtosis()),
            "best_day": {
                "date": str(df.loc[best_idx, "datetime"].date()),
                "return": self._safe_float(nonzero_returns.loc[best_idx]),
            },
            "worst_day": {
                "date": str(df.loc[worst_idx, "datetime"].date()),
                "return": self._safe_float(nonzero_returns.loc[worst_idx]),
            },
            "positive_day_ratio": self._safe_float((nonzero_returns > 0).mean()),
            "total_return": self._safe_float(total_return),
        }

    def _drawdown_profile(self, df: pd.DataFrame, wealth: pd.Series, drawdown: pd.Series) -> dict[str, Any]:
        end_idx = int(drawdown.idxmin())
        start_idx = int(wealth.loc[:end_idx].idxmax())
        top_periods = self._drawdown_periods(df=df, drawdown=drawdown)
        return {
            "max_drawdown": self._safe_float(drawdown.iloc[end_idx]),
            "max_drawdown_start": str(df.loc[start_idx, "datetime"].date()),
            "max_drawdown_end": str(df.loc[end_idx, "datetime"].date()),
            "max_drawdown_duration_bars": int(end_idx - start_idx + 1),
            "current_drawdown": self._safe_float(drawdown.iloc[-1]),
            "top_drawdown_periods": top_periods[:5],
        }

    def _drawdown_periods(self, df: pd.DataFrame, drawdown: pd.Series) -> list[dict[str, Any]]:
        periods: list[dict[str, Any]] = []
        in_period = False
        start = 0
        trough = 0
        for i, value in enumerate(drawdown):
            if value < 0 and not in_period:
                in_period = True
                start = max(i - 1, 0)
                trough = i
            if in_period and value < drawdown.iloc[trough]:
                trough = i
            if in_period and (value >= 0 or i == len(drawdown) - 1):
                end = i
                periods.append(
                    {
                        "start": str(df.loc[start, "datetime"].date()),
                        "trough": str(df.loc[trough, "datetime"].date()),
                        "end": str(df.loc[end, "datetime"].date()),
                        "max_drawdown": self._safe_float(drawdown.iloc[trough]),
                        "duration_bars": int(end - start + 1),
                    }
                )
                in_period = False
        return sorted(periods, key=lambda item: item["max_drawdown"])

    def _trend_profile(self, df: pd.DataFrame) -> dict[str, Any]:
        close = df["close"]
        result: dict[str, Any] = {}
        for window in [20, 60, 120]:
            ma = close.rolling(window).mean()
            result[f"ma{window}_slope"] = self._safe_float((ma.iloc[-1] - ma.dropna().iloc[0]) / ma.dropna().iloc[0]) if not ma.dropna().empty else None
            result[f"ma{window}_slope_positive_ratio"] = self._safe_float((ma.diff() > 0).dropna().mean())
            result[f"close_above_ma{window}_ratio"] = self._safe_float((close > ma).dropna().mean())

        total_return = close.iloc[-1] / close.iloc[0] - 1.0
        ma60_pos = result.get("ma60_slope_positive_ratio") or 0.0
        above60 = result.get("close_above_ma60_ratio") or 0.0
        if total_return >= 0.08 and ma60_pos >= 0.55 and above60 >= 0.50:
            label, label_zh = "uptrend", "上行趋势"
        elif total_return <= -0.08 and ma60_pos <= 0.45 and above60 <= 0.50:
            label, label_zh = "downtrend", "下行趋势"
        elif abs(total_return) <= 0.06:
            label, label_zh = "range", "震荡"
        elif total_return > 0:
            label, label_zh = "weak_uptrend", "弱上行"
        else:
            label, label_zh = "weak_downtrend", "弱下行"
        result.update(
            {
                "trend_label": label,
                "trend_label_zh": label_zh,
                "description": self._overall_trend_description(df=df, label_zh=label_zh, total_return=total_return, result=result),
            }
        )
        return result

    def _overall_trend_description(self, df: pd.DataFrame, label_zh: str, total_return: float, result: dict[str, Any]) -> str:
        start = str(df["datetime"].iloc[0].date())
        end = str(df["datetime"].iloc[-1].date())
        above60 = result.get("close_above_ma60_ratio")
        ma60_pos = result.get("ma60_slope_positive_ratio")
        return (
            f"{start} 至 {end} 样本期累计收益为 {self._pct(total_return)}，"
            f"60日均线斜率为正的交易日占比为 {self._pct(ma60_pos)}，"
            f"收盘价位于60日均线上方的交易日占比为 {self._pct(above60)}，"
            f"整体表现为{label_zh}特征。"
        )

    def _volatility_profile(self, df: pd.DataFrame, returns: pd.Series) -> dict[str, Any]:
        vol20 = returns.rolling(20).std() * math.sqrt(252.0)
        vol60 = returns.rolling(60).std() * math.sqrt(252.0)
        latest20 = self._safe_float(vol20.dropna().iloc[-1]) if not vol20.dropna().empty else None
        mean20 = self._safe_float(vol20.mean())
        q75 = self._safe_float(vol20.quantile(0.75))
        if latest20 is None or q75 is None:
            regime = "unknown"
            regime_zh = "未知"
        elif latest20 > q75 * 1.2:
            regime = "high"
            regime_zh = "高波动"
        elif latest20 < (vol20.quantile(0.25) or 0):
            regime = "low"
            regime_zh = "低波动"
        else:
            regime = "normal"
            regime_zh = "常规波动"
        high_periods = self._volatility_periods(df=df, vol=vol20, threshold=q75)
        atr_proxy = ((df["high"] - df["low"]) / df["close"]).rolling(20).mean()
        return {
            "volatility_20d_mean": mean20,
            "volatility_60d_mean": self._safe_float(vol60.mean()),
            "volatility_20d_latest": latest20,
            "volatility_20d_q75": q75,
            "volatility_regime": regime,
            "volatility_regime_zh": regime_zh,
            "atr_proxy_20d_latest": self._safe_float(atr_proxy.dropna().iloc[-1]) if not atr_proxy.dropna().empty else None,
            "high_volatility_periods": high_periods,
        }

    def _volatility_periods(self, df: pd.DataFrame, vol: pd.Series, threshold: float | None) -> list[dict[str, Any]]:
        if threshold is None:
            return []
        result: list[dict[str, Any]] = []
        active = False
        start = 0
        values = vol.fillna(0)
        for i, value in enumerate(values):
            if value > threshold and not active:
                active = True
                start = i
            if active and (value <= threshold or i == len(values) - 1):
                end = i
                if end - start + 1 >= 5:
                    result.append(
                        {
                            "start": str(df.loc[start, "datetime"].date()),
                            "end": str(df.loc[end, "datetime"].date()),
                            "max_annualized_volatility": self._safe_float(values.iloc[start : end + 1].max()),
                        }
                    )
                active = False
        return result[:5]

    def _regime_segments(self, df: pd.DataFrame, smooth_window: int, min_segment_days: int) -> list[dict[str, Any]]:
        enriched = df.copy()
        close = enriched["close"]
        returns = close.pct_change().fillna(0.0)
        return_from_start = close / close.iloc[0] - 1.0
        enriched["_return_20"] = close.pct_change(20).fillna(return_from_start)
        enriched["_return_60"] = close.pct_change(60).fillna(return_from_start)
        enriched["_ma20"] = close.rolling(20, min_periods=5).mean()
        enriched["_ma60"] = close.rolling(60, min_periods=10).mean()
        enriched["_ma20_slope_5"] = enriched["_ma20"].pct_change(5)
        enriched["_ma60_slope_10"] = enriched["_ma60"].pct_change(10)
        enriched["_above_ma20"] = (close > enriched["_ma20"]).where(enriched["_ma20"].notna())
        enriched["_above_ma60"] = (close > enriched["_ma60"]).where(enriched["_ma60"].notna())
        enriched["_volatility_20"] = returns.rolling(20, min_periods=5).std(ddof=0) * math.sqrt(252.0)
        vol_threshold = self._safe_float(enriched["_volatility_20"].quantile(0.75))
        enriched["_high_volatility"] = enriched["_volatility_20"] > (vol_threshold or float("inf"))

        raw_labels = [self._classify_regime_day(row, vol_threshold=vol_threshold) for _, row in enriched.iterrows()]
        smoothed_labels = self._smooth_labels(raw_labels, smooth_window=smooth_window)
        raw_segments = self._labels_to_segments(smoothed_labels)
        merged_segments = self._merge_short_segments(raw_segments, min_segment_days=min_segment_days)
        return [self._build_regime_segment(enriched, item) for item in merged_segments]

    def _classify_regime_day(self, row: pd.Series, vol_threshold: float | None) -> str:
        ret20 = self._finite_float(row.get("_return_20"), default=0.0)
        ret60 = self._finite_float(row.get("_return_60"), default=0.0)
        ma20_slope = self._finite_float(row.get("_ma20_slope_5"), default=0.0)
        ma60_slope = self._finite_float(row.get("_ma60_slope_10"), default=0.0)
        above20 = bool(row.get("_above_ma20")) if pd.notna(row.get("_above_ma20")) else False
        above60 = bool(row.get("_above_ma60")) if pd.notna(row.get("_above_ma60")) else False

        up_score = sum(
            [
                ret20 >= 0.03,
                ret60 >= 0.06,
                ma20_slope > 0,
                ma60_slope > 0,
                above20,
                above60,
            ]
        )
        down_score = sum(
            [
                ret20 <= -0.03,
                ret60 <= -0.06,
                ma20_slope < 0,
                ma60_slope < 0,
                not above20,
                not above60,
            ]
        )

        if up_score >= 4 and (ret20 >= 0.08 or ret60 >= 0.10):
            label = "strong_uptrend"
        elif up_score >= 3 and ret20 > 0:
            label = "weak_uptrend"
        elif down_score >= 4 and (ret20 <= -0.08 or ret60 <= -0.10):
            label = "strong_downtrend"
        elif down_score >= 3 and ret20 < 0:
            label = "weak_downtrend"
        else:
            label = "range"

        vol20 = self._finite_float(row.get("_volatility_20"), default=None)
        high_vol = vol_threshold is not None and vol20 is not None and vol20 > vol_threshold and vol20 > 0
        if not high_vol:
            return label
        if "uptrend" in label:
            return "high_volatility_uptrend"
        if "downtrend" in label:
            return "high_volatility_downtrend"
        return "high_volatility_range"

    def _smooth_labels(self, labels: list[str], smooth_window: int) -> list[str]:
        window = max(int(smooth_window), 1)
        if window <= 1:
            return labels
        result: list[str] = []
        for i, current in enumerate(labels):
            recent = labels[max(0, i - window + 1) : i + 1]
            counts = {label: recent.count(label) for label in set(recent)}
            result.append(max(counts, key=lambda label: (counts[label], label == current)))
        return result

    def _labels_to_segments(self, labels: list[str]) -> list[dict[str, Any]]:
        if not labels:
            return []
        segments: list[dict[str, Any]] = []
        start = 0
        current = labels[0]
        for i, label in enumerate(labels[1:], start=1):
            if label != current:
                segments.append({"start_idx": start, "end_idx": i - 1, "label": current})
                start = i
                current = label
        segments.append({"start_idx": start, "end_idx": len(labels) - 1, "label": current})
        return segments

    def _merge_short_segments(self, segments: list[dict[str, Any]], min_segment_days: int) -> list[dict[str, Any]]:
        minimum = max(int(min_segment_days), 1)
        result = [item.copy() for item in segments]
        while len(result) > 1:
            short_index = next(
                (i for i, item in enumerate(result) if item["end_idx"] - item["start_idx"] + 1 < minimum),
                None,
            )
            if short_index is None:
                break
            target_index = self._short_segment_target(result, short_index)
            current = result[short_index]
            target = result[target_index]
            merged = {
                "start_idx": min(current["start_idx"], target["start_idx"]),
                "end_idx": max(current["end_idx"], target["end_idx"]),
                "label": target["label"],
            }
            for index in sorted([short_index, target_index], reverse=True):
                result.pop(index)
            insert_at = min(short_index, target_index)
            result.insert(insert_at, merged)
            result = self._compact_adjacent_segments(result)
        return result

    def _short_segment_target(self, segments: list[dict[str, Any]], index: int) -> int:
        candidates: list[tuple[int, int, int]] = []
        current = segments[index]
        if index > 0:
            prev = segments[index - 1]
            candidates.append((self._label_distance(current["label"], prev["label"]), -self._segment_days(prev), index - 1))
        if index < len(segments) - 1:
            nxt = segments[index + 1]
            candidates.append((self._label_distance(current["label"], nxt["label"]), -self._segment_days(nxt), index + 1))
        return min(candidates)[2]

    def _compact_adjacent_segments(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not segments:
            return []
        compacted = [segments[0]]
        for item in segments[1:]:
            last = compacted[-1]
            if last["label"] == item["label"]:
                last["end_idx"] = item["end_idx"]
            else:
                compacted.append(item)
        return compacted

    def _label_distance(self, left: str, right: str) -> int:
        if left == right:
            return 0
        left_family = self._label_family(left)
        right_family = self._label_family(right)
        if left_family == right_family:
            return 1
        if "range" in {left_family, right_family}:
            return 2
        return 3

    def _label_family(self, label: str) -> str:
        if "uptrend" in label:
            return "uptrend"
        if "downtrend" in label:
            return "downtrend"
        return "range"

    def _segment_days(self, segment: dict[str, Any]) -> int:
        return int(segment["end_idx"] - segment["start_idx"] + 1)

    def _build_regime_segment(self, enriched: pd.DataFrame, item: dict[str, Any]) -> dict[str, Any]:
        start_idx = int(item["start_idx"])
        end_idx = int(item["end_idx"])
        part = enriched.iloc[start_idx : end_idx + 1].reset_index(drop=True)
        returns = part["close"].pct_change().fillna(0.0)
        total_return = float(part["close"].iloc[-1] / part["close"].iloc[0] - 1.0) if len(part) > 1 else 0.0
        volatility = float(returns.iloc[1:].std(ddof=0) * math.sqrt(252.0)) if len(part) > 2 else 0.0
        wealth = (1.0 + returns).cumprod()
        max_drawdown = float((wealth / wealth.cummax() - 1.0).min()) if len(part) > 1 else 0.0
        ma20_slope = self._slope_from_series(part["_ma20"])
        ma60_slope = self._slope_from_series(part["_ma60"])
        ma20_slope_positive_ratio = self._true_ratio(part["_ma20_slope_5"] > 0, valid_mask=part["_ma20_slope_5"].notna())
        ma60_slope_positive_ratio = self._true_ratio(part["_ma60_slope_10"] > 0, valid_mask=part["_ma60_slope_10"].notna())
        close_above_ma20_ratio = self._true_ratio(part["_above_ma20"], valid_mask=part["_above_ma20"].notna())
        close_above_ma60_ratio = self._true_ratio(part["_above_ma60"], valid_mask=part["_above_ma60"].notna())
        high_volatility_day_ratio = self._true_ratio(part["_high_volatility"], valid_mask=part["_volatility_20"].notna())
        label = self._segment_label_from_stats(
            total_return=total_return,
            ma20_slope=ma20_slope,
            ma60_slope=ma60_slope,
            close_above_ma20_ratio=close_above_ma20_ratio,
            close_above_ma60_ratio=close_above_ma60_ratio,
            high_volatility_day_ratio=high_volatility_day_ratio,
        )
        segment = {
            "start": str(enriched.loc[start_idx, "datetime"].date()),
            "end": str(enriched.loc[end_idx, "datetime"].date()),
            "trading_days": int(len(part)),
            "label": label,
            "label_zh": self._label_zh(label),
            "return": self._safe_float(total_return),
            "annualized_volatility": self._safe_float(volatility),
            "volatility": self._safe_float(volatility),
            "max_drawdown": self._safe_float(max_drawdown),
            "positive_day_ratio": self._safe_float((returns.iloc[1:] > 0).mean()) if len(part) > 1 else None,
            "ma20_slope": ma20_slope,
            "ma60_slope": ma60_slope,
            "ma20_slope_positive_ratio": ma20_slope_positive_ratio,
            "ma60_slope_positive_ratio": ma60_slope_positive_ratio,
            "close_above_ma20_ratio": close_above_ma20_ratio,
            "close_above_ma60_ratio": close_above_ma60_ratio,
            "high_volatility_day_ratio": high_volatility_day_ratio,
        }
        segment["description"] = self._segment_description(segment)
        return segment

    def _segment_label_from_stats(
        self,
        total_return: float,
        ma20_slope: float | None,
        ma60_slope: float | None,
        close_above_ma20_ratio: float | None,
        close_above_ma60_ratio: float | None,
        high_volatility_day_ratio: float | None,
    ) -> str:
        ma20 = ma20_slope or 0.0
        ma60 = ma60_slope or 0.0
        above20 = close_above_ma20_ratio or 0.0
        above60 = close_above_ma60_ratio or 0.0
        high_vol = (high_volatility_day_ratio or 0.0) >= 0.50
        up_score = sum([total_return >= 0.025, ma20 > 0, ma60 > 0, above20 >= 0.50, above60 >= 0.50])
        down_score = sum([total_return <= -0.025, ma20 < 0, ma60 < 0, above20 <= 0.50, above60 <= 0.50])
        if up_score >= 4 and total_return >= 0.06:
            base = "strong_uptrend"
        elif up_score >= 3 and total_return > 0:
            base = "weak_uptrend"
        elif down_score >= 4 and total_return <= -0.06:
            base = "strong_downtrend"
        elif down_score >= 3 and total_return < 0:
            base = "weak_downtrend"
        else:
            base = "range"
        if not high_vol:
            return base
        if "uptrend" in base:
            return "high_volatility_uptrend"
        if "downtrend" in base:
            return "high_volatility_downtrend"
        return "high_volatility_range"

    def _label_zh(self, label: str) -> str:
        mapping = {
            "strong_uptrend": "强上行阶段",
            "weak_uptrend": "弱上行阶段",
            "range": "震荡阶段",
            "weak_downtrend": "弱下行阶段",
            "strong_downtrend": "强下行阶段",
            "high_volatility_uptrend": "高波动上行阶段",
            "high_volatility_downtrend": "高波动下行阶段",
            "high_volatility_range": "高波动震荡阶段",
        }
        return mapping.get(label, label)

    def _slope_from_series(self, series: pd.Series) -> float | None:
        values = pd.to_numeric(series, errors="coerce").dropna()
        if len(values) < 2:
            return None
        first = float(values.iloc[0])
        last = float(values.iloc[-1])
        if first == 0:
            return None
        return self._safe_float(last / first - 1.0)

    def _true_ratio(self, series: pd.Series, valid_mask: pd.Series | None = None) -> float | None:
        values = series.copy()
        if valid_mask is not None:
            values = values[valid_mask]
        values = values.dropna()
        if values.empty:
            return None
        return self._safe_float(values.astype(bool).mean())

    def _finite_float(self, value: Any, default: float | None) -> float | None:
        number = self._safe_float(value)
        return default if number is None else number

    def _segment_description(self, segment: dict[str, Any]) -> str:
        return (
            f"{segment['start']} 至 {segment['end']} 共 {segment['trading_days']} 个交易日，"
            f"区间累计收益为 {self._pct(segment['return'])}，"
            f"年化波动率为 {self._pct(segment['annualized_volatility'])}，"
            f"最大回撤为 {self._pct(segment['max_drawdown'])}，"
            f"20日均线斜率为正的交易日占比为 {self._pct(segment['ma20_slope_positive_ratio'])}，"
            f"收盘价位于20日均线上方的交易日占比为 {self._pct(segment['close_above_ma20_ratio'])}，"
            f"表现为{segment['label_zh']}。"
        )

    def _liquidity_profile(self, df: pd.DataFrame) -> dict[str, Any]:
        result: dict[str, Any] = {"has_volume": "volume" in df.columns, "has_amount": "amount" in df.columns}
        if "volume" in df.columns:
            volume = pd.to_numeric(df["volume"], errors="coerce")
            result.update(
                {
                    "volume_mean": self._safe_float(volume.mean()),
                    "volume_median": self._safe_float(volume.median()),
                    "volume_latest": self._safe_float(volume.iloc[-1]),
                    "volume_spike_days": self._spike_days(df=df, series=volume, column_name="volume"),
                }
            )
        if "amount" in df.columns:
            amount = pd.to_numeric(df["amount"], errors="coerce")
            result.update(
                {
                    "amount_mean": self._safe_float(amount.mean()),
                    "amount_median": self._safe_float(amount.median()),
                    "amount_latest": self._safe_float(amount.iloc[-1]),
                }
            )
        if not result["has_volume"] and not result["has_amount"]:
            result["liquidity_note"] = "数据中未提供 volume 或 amount 字段，无法形成成交量或成交额画像。"
        return result

    def _spike_days(self, df: pd.DataFrame, series: pd.Series, column_name: str) -> list[dict[str, Any]]:
        threshold = series.mean() + 2 * series.std(ddof=0)
        spikes = df.loc[series > threshold, ["datetime"]].copy()
        spikes[column_name] = series[series > threshold]
        return [
            {"date": str(row["datetime"].date()), column_name: self._safe_float(row[column_name])}
            for _, row in spikes.head(10).iterrows()
        ]

    def _risk_events(self, df: pd.DataFrame, returns: pd.Series, drawdown_profile: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for period in drawdown_profile.get("top_drawdown_periods", [])[:3]:
            events.append(
                {
                    "type": "major_drawdown",
                    **period,
                    "description": (
                        f"{period['start']} 至 {period['end']} 出现回撤阶段，"
                        f"区间最低点在 {period['trough']}，最大回撤为 {self._pct(period['max_drawdown'])}。"
                    ),
                }
            )
        worst_idx = returns.iloc[1:].idxmin() if len(returns) > 1 else returns.idxmin()
        events.append(
            {
                "type": "worst_day",
                "date": str(df.loc[worst_idx, "datetime"].date()),
                "return": self._safe_float(returns.loc[worst_idx]),
                "description": f"{str(df.loc[worst_idx, 'datetime'].date())} 为样本期最差单日，日收益为 {self._pct(returns.loc[worst_idx])}。",
            }
        )
        return events

    def _profile_flags(
        self,
        return_profile: dict[str, Any],
        drawdown_profile: dict[str, Any],
        trend_profile: dict[str, Any],
        volatility_profile: dict[str, Any],
    ) -> dict[str, Any]:
        max_dd = abs(drawdown_profile.get("max_drawdown") or 0.0)
        annual_vol = return_profile.get("annualized_volatility") or 0.0
        return {
            "trend_label": trend_profile.get("trend_label"),
            "trend_label_zh": trend_profile.get("trend_label_zh"),
            "volatility_level": self._level(annual_vol, low=0.12, high=0.25),
            "drawdown_risk_level": self._level(max_dd, low=0.08, high=0.18),
            "return_direction": "positive" if (return_profile.get("total_return") or 0.0) > 0 else "negative",
            "volatility_regime": volatility_profile.get("volatility_regime"),
        }

    def _fact_descriptions(
        self,
        summary: dict[str, Any],
        return_profile: dict[str, Any],
        drawdown_profile: dict[str, Any],
        trend_profile: dict[str, Any],
        volatility_profile: dict[str, Any],
        regime_segments: list[dict[str, Any]],
    ) -> list[str]:
        descriptions = [
            f"样本期为 {summary['start_date']} 至 {summary['end_date']}，共 {summary['row_count']} 个交易日。",
            f"样本期累计收益为 {self._pct(return_profile['total_return'])}，年化波动率为 {self._pct(return_profile['annualized_volatility'])}。",
            f"最大回撤为 {self._pct(drawdown_profile['max_drawdown'])}，发生在 {drawdown_profile['max_drawdown_start']} 至 {drawdown_profile['max_drawdown_end']}。",
            trend_profile["description"],
            f"最新20日年化波动率为 {self._pct(volatility_profile['volatility_20d_latest'])}，波动状态标记为{volatility_profile['volatility_regime_zh']}。",
        ]
        if regime_segments:
            counts = pd.Series([item["label_zh"] for item in regime_segments]).value_counts().to_dict()
            rendered = "，".join([f"{key} {value} 段" for key, value in counts.items()])
            descriptions.append(f"按自适应走势划分的阶段中，包含 {rendered}。")
        return descriptions

    def _format_markdown(self, profile: dict[str, Any]) -> str:
        lines = [
            "# Market Profile",
            "",
        ]
        chart_filename = profile.get("visualizations", {}).get("price_regime_chart_filename")
        if chart_filename:
            lines.extend(
                [
                    "## Chart",
                    f"![Market Profile Chart]({chart_filename})",
                    "",
                ]
            )
        lines.extend(
            [
            "## Summary",
            *[f"- {key}: {value}" for key, value in profile["summary"].items() if key != "columns"],
            f"- columns: {', '.join(profile['summary']['columns'])}",
            "",
            "## Fact Descriptions",
            *[f"- {item}" for item in profile["fact_descriptions"]],
            "",
            "## Return Profile",
            *[f"- {key}: {self._render_value(value)}" for key, value in profile["return_profile"].items()],
            "",
            "## Drawdown Profile",
            *[f"- {key}: {self._render_value(value)}" for key, value in profile["drawdown_profile"].items() if key != "top_drawdown_periods"],
            "",
            "## Trend Profile",
            *[f"- {key}: {self._render_value(value)}" for key, value in profile["trend_profile"].items()],
            "",
            "## Volatility Profile",
            *[f"- {key}: {self._render_value(value)}" for key, value in profile["volatility_profile"].items() if key != "high_volatility_periods"],
            "",
            "## Regime Segments",
            ]
        )
        for segment in profile["regime_segments"]:
            lines.append(f"- {segment['description']}")
        lines.extend(["", "## Risk Events"])
        for event in profile["risk_events"]:
            lines.append(f"- {event['description']}")
        lines.extend(["", "## Profile Flags"])
        lines.extend([f"- {key}: {value}" for key, value in profile["profile_flags"].items()])
        lines.append("")
        return "\n".join(lines)

    def _write_chart(self, df: pd.DataFrame, profile: dict[str, Any], chart_path: Path) -> None:
        try:
            import matplotlib

            matplotlib.use("Agg", force=True)
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise RuntimeError("生成市场画像图像需要 matplotlib。") from exc

        chart_df = df.copy()
        chart_df["ma20"] = chart_df["close"].rolling(20, min_periods=5).mean()
        chart_df["ma60"] = chart_df["close"].rolling(60, min_periods=10).mean()
        returns = chart_df["close"].pct_change().fillna(0.0)
        wealth = (1.0 + returns).cumprod()
        drawdown = wealth / wealth.cummax() - 1.0

        has_volume = "volume" in chart_df.columns
        if has_volume:
            fig, axes = plt.subplots(
                3,
                1,
                figsize=(14, 10),
                sharex=True,
                gridspec_kw={"height_ratios": [3.2, 1.2, 1.0]},
            )
            ax_price, ax_drawdown, ax_volume = axes
        else:
            fig, axes = plt.subplots(
                2,
                1,
                figsize=(14, 8),
                sharex=True,
                gridspec_kw={"height_ratios": [3.2, 1.2]},
            )
            ax_price, ax_drawdown = axes
            ax_volume = None

        dates = chart_df["datetime"]
        for segment in profile.get("regime_segments", []):
            start = pd.to_datetime(segment["start"])
            end = pd.to_datetime(segment["end"])
            color = self._segment_color(segment.get("label"))
            ax_price.axvspan(start, end, color=color, alpha=0.12, linewidth=0)
            ax_drawdown.axvspan(start, end, color=color, alpha=0.08, linewidth=0)
            if ax_volume is not None:
                ax_volume.axvspan(start, end, color=color, alpha=0.08, linewidth=0)

        ax_price.plot(dates, chart_df["close"], label="Close", color="#1f2937", linewidth=1.6)
        ax_price.plot(dates, chart_df["ma20"], label="MA20", color="#2563eb", linewidth=1.0, alpha=0.9)
        ax_price.plot(dates, chart_df["ma60"], label="MA60", color="#f97316", linewidth=1.0, alpha=0.9)
        ax_price.set_title(self._chart_title(profile), loc="left", fontsize=13, fontweight="bold")
        ax_price.set_ylabel("Price")
        ax_price.grid(True, alpha=0.25)
        ax_price.legend(loc="upper left", ncols=3, frameon=False)

        worst_day = next((event for event in profile.get("risk_events", []) if event.get("type") == "worst_day"), None)
        if worst_day:
            worst_date = pd.to_datetime(worst_day["date"])
            matched = chart_df.loc[chart_df["datetime"] == worst_date]
            if not matched.empty:
                price = matched["close"].iloc[0]
                ax_price.scatter([worst_date], [price], color="#dc2626", s=42, zorder=5)
                ax_price.annotate(
                    "Worst day",
                    xy=(worst_date, price),
                    xytext=(8, -18),
                    textcoords="offset points",
                    fontsize=8,
                    color="#991b1b",
                    arrowprops={"arrowstyle": "->", "color": "#991b1b", "linewidth": 0.8},
                )

        ax_drawdown.fill_between(dates, drawdown, 0, color="#dc2626", alpha=0.25)
        ax_drawdown.plot(dates, drawdown, color="#991b1b", linewidth=0.9)
        ax_drawdown.set_ylabel("Drawdown")
        ax_drawdown.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")
        ax_drawdown.grid(True, alpha=0.25)
        max_dd_end = profile.get("drawdown_profile", {}).get("max_drawdown_end")
        if max_dd_end:
            ax_drawdown.axvline(pd.to_datetime(max_dd_end), color="#7f1d1d", linestyle="--", linewidth=0.8, alpha=0.8)

        if ax_volume is not None:
            ax_volume.bar(dates, chart_df["volume"], color="#64748b", width=1.0, alpha=0.55)
            ax_volume.set_ylabel("Volume")
            ax_volume.grid(True, axis="y", alpha=0.2)

        locator = mdates.AutoDateLocator(minticks=5, maxticks=10)
        ax_drawdown.xaxis.set_major_locator(locator)
        ax_drawdown.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        fig.text(
            0.01,
            0.01,
            "Background colors show adaptive regime segments. This chart is deterministic and does not contain strategy advice.",
            fontsize=8,
            color="#475569",
        )
        fig.tight_layout(rect=(0, 0.025, 1, 1))
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(chart_path, dpi=160, bbox_inches="tight")
        plt.close(fig)

    def _segment_color(self, label: str | None) -> str:
        if label in {"strong_uptrend", "weak_uptrend", "high_volatility_uptrend"}:
            return "#16a34a"
        if label in {"strong_downtrend", "weak_downtrend", "high_volatility_downtrend"}:
            return "#dc2626"
        if label == "high_volatility_range":
            return "#a855f7"
        return "#94a3b8"

    def _chart_title(self, profile: dict[str, Any]) -> str:
        summary = profile.get("summary", {})
        symbol = summary.get("symbol") or "Asset"
        start = summary.get("start_date")
        end = summary.get("end_date")
        total_return = self._pct(profile.get("return_profile", {}).get("total_return"))
        max_drawdown = self._pct(profile.get("drawdown_profile", {}).get("max_drawdown"))
        return f"{symbol} Market Profile ({start} to {end}) | Return {total_return}, Max DD {max_drawdown}"

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
            raise ValueError(f"数据缺少画像必需字段：{missing}")
        normalized = df.copy()
        normalized["datetime"] = self._parse_datetime(normalized["datetime"])
        for column in ["open", "high", "low", "close", "volume", "amount"]:
            if column in normalized:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
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

    def _resolve_data_path(self, data_path: Path | None, run_state: dict[str, Any] | None) -> Path:
        if data_path:
            return self._resolve_path(data_path)
        if run_state:
            candidate = run_state.get("steps", {}).get("data_acquisition", {}).get("primary_dataset")
            if not candidate:
                candidate = run_state.get("artifacts", {}).get("datasets", {}).get("primary", {}).get("dataset_path")
            if candidate:
                return self._resolve_path(Path(candidate))
        raise ValueError("必须提供 DATA_PATH，或提供包含 steps.data_acquisition.primary_dataset 的 run_state.json。")

    def _resolve_output_dir(self, output_dir: Path | None, profile_id: str, run_state: dict[str, Any] | None) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        if run_state:
            market_profile_dir = run_state.get("directories", {}).get("market_profile")
            if market_profile_dir:
                return self._resolve_path(Path(market_profile_dir))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.config.root_dir / "artifacts" / "market_profiles" / f"{profile_id}_{timestamp}"

    def _load_run_state(self, run_state_path: Path | None) -> dict[str, Any] | None:
        if not run_state_path:
            return None
        path = self._resolve_path(run_state_path)
        if not path.exists():
            raise FileNotFoundError(f"run_state.json 不存在：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _update_run_state(
        self,
        run_state_path: Path,
        profile: dict[str, Any],
        json_path: Path | None,
        markdown_path: Path | None,
        chart_path: Path | None,
    ) -> None:
        path = self._resolve_path(run_state_path)
        state = json.loads(path.read_text(encoding="utf-8"))
        now = datetime.now().isoformat()
        market_profile = state.setdefault("steps", {}).setdefault("market_profile", {})
        market_profile.update(
            {
                "status": "success",
                "profile_path": self._relative(json_path) if json_path else None,
                "profile_md_path": self._relative(markdown_path) if markdown_path else None,
                "chart_path": self._relative(chart_path) if chart_path else None,
                "summary": {
                    "trend_label": profile["trend_profile"].get("trend_label"),
                    "trend_label_zh": profile["trend_profile"].get("trend_label_zh"),
                    "total_return": profile["return_profile"].get("total_return"),
                    "max_drawdown": profile["drawdown_profile"].get("max_drawdown"),
                    "volatility_regime": profile["volatility_profile"].get("volatility_regime"),
                },
                "error": None,
                "finished_at": now,
            }
        )
        artifacts = state.setdefault("artifacts", {}).setdefault("market_profile", {})
        artifacts["primary"] = {
            "json_path": self._relative(json_path) if json_path else None,
            "markdown_path": self._relative(markdown_path) if markdown_path else None,
            "chart_path": self._relative(chart_path) if chart_path else None,
            "created_at": now,
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "MarketProfileService",
                "event": "market_profile_completed",
                "summary": "市场画像已生成。",
            }
        )
        state["updated_at"] = now
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _resolve_path(self, path: str | Path | None) -> Path:
        if path is None:
            raise ValueError("path 不能为空。")
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return str(path.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(path)

    def _level(self, value: float, low: float, high: float) -> str:
        if value < low:
            return "low"
        if value > high:
            return "high"
        return "medium"

    def _pct(self, value: Any) -> str:
        if value is None:
            return "未知"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "未知"
        if math.isnan(number) or math.isinf(number):
            return "未知"
        return f"{number:.2%}"

    def _safe_float(self, value: Any) -> float | None:
        if hasattr(value, "item"):
            value = value.item()
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    def _render_value(self, value: Any) -> str:
        if isinstance(value, float):
            return f"{value:.6f}"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
