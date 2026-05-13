from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from strategy_lab.schemas.data import DatasetArtifact


STANDARD_COLUMNS = [
    "symbol",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "source",
]

DEFAULT_ALIASES = {
    "datetime": ["datetime", "date", "trade_date", "time", "timestamp", "日期", "交易日期"],
    "open": ["open", "Open", "开盘", "开盘价"],
    "high": ["high", "High", "最高", "最高价"],
    "low": ["low", "Low", "最低", "最低价"],
    "close": ["close", "Close", "收盘", "收盘价"],
    "volume": ["volume", "vol", "Volume", "成交量"],
    "amount": ["amount", "amt", "Amount", "成交额"],
    "adj_factor": ["adj_factor", "factor", "复权因子"],
    "symbol": ["symbol", "code", "ts_code", "证券代码", "股票代码"],
}


class DataService:
    """数据服务：负责把不同来源的行情统一成项目标准数据产物。"""

    def load_csv(
        self,
        csv_path: str | Path,
        output_dir: str | Path,
        symbol: str | None = None,
        source: str = "csv",
        encoding: str | None = None,
        mapping: dict[str, str] | None = None,
    ) -> DatasetArtifact:
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CSV file not found: {path}")
        df = pd.read_csv(path, encoding=encoding)
        normalized = self.normalize_ohlcv(df, symbol=symbol, source=source, mapping=mapping)
        return self.save_standard_dataset(
            normalized,
            output_dir=output_dir,
            source_used=[source],
            metadata_extra={"input_path": str(path)},
        )

    def load_miniqmt_ohlcv(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        output_dir: str | Path,
        period: str = "1d",
        adjust: str = "qfq",
        download: bool = True,
    ) -> DatasetArtifact:
        """从 MiniQMT/xtdata 获取历史 K 线并保存为标准数据产物。

        MiniQMT 依赖本机 QMT 客户端和 xtquant 环境。默认会先调用
        download_history_data 补齐本地缓存，再用 get_market_data_ex 读取。
        """
        try:
            from xtquant import xtdata
        except Exception as exc:  # pragma: no cover - depends on local QMT env
            raise RuntimeError("未能导入 xtquant，请确认 MiniQMT/xtquant 已安装。") from exc

        start_time = self._to_xt_time(start_date)
        end_time = self._to_xt_time(end_date)
        dividend_type = self._to_xt_dividend_type(adjust)

        if download:
            try:
                xtdata.download_history_data(
                    symbol,
                    period=period,
                    start_time=start_time,
                    end_time=end_time,
                    incrementally=True,
                )
            except Exception as exc:  # pragma: no cover - depends on local QMT env
                raise RuntimeError(
                    "MiniQMT 历史行情下载失败。请确认 QMT 已启动、已登录，且 xtdata 可连接行情服务。"
                ) from exc

        try:
            raw = xtdata.get_market_data_ex(
                [],
                [symbol],
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=-1,
                dividend_type=dividend_type,
                fill_data=True,
            )
        except Exception as exc:  # pragma: no cover - depends on local QMT env
            raise RuntimeError(
                "MiniQMT 历史行情读取失败。请确认本地行情已下载且 QMT 行情服务可用。"
            ) from exc

        if not raw or symbol not in raw:
            raise ValueError(f"MiniQMT 未返回 {symbol} 的行情数据。")

        df = raw[symbol]
        if df is None or len(df) == 0:
            raise ValueError(f"MiniQMT 返回的 {symbol} 行情为空。")

        df = df.reset_index()
        normalized = self.normalize_ohlcv(
            df,
            symbol=symbol,
            source="miniqmt",
            mapping=self._build_miniqmt_mapping(df),
        )
        return self.save_standard_dataset(
            normalized,
            output_dir=output_dir,
            source_used=["miniqmt"],
            metadata_extra={
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "period": period,
                "adjust": adjust,
            },
        )

    def normalize_ohlcv(
        self,
        df: pd.DataFrame,
        symbol: str | None = None,
        source: str = "unknown",
        mapping: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        if df.empty:
            raise ValueError("Input data is empty.")

        column_map = self._infer_column_map(df.columns, mapping=mapping)
        required = ["datetime", "open", "high", "low", "close"]
        missing = [column for column in required if column not in column_map]
        if missing:
            raise ValueError(f"Missing required OHLCV columns: {missing}")

        normalized = pd.DataFrame()
        for standard, original in column_map.items():
            normalized[standard] = df[original]

        if "symbol" not in normalized:
            if not symbol:
                raise ValueError("Missing symbol column and no symbol argument supplied.")
            normalized["symbol"] = symbol

        normalized["datetime"] = pd.to_datetime(normalized["datetime"])
        for column in ["open", "high", "low", "close", "volume", "amount", "adj_factor"]:
            if column not in normalized:
                normalized[column] = None
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

        normalized["source"] = source
        normalized = normalized[STANDARD_COLUMNS].sort_values("datetime").reset_index(drop=True)
        return normalized

    def _to_xt_time(self, value: str) -> str:
        return value.replace("-", "")

    def _to_xt_dividend_type(self, adjust: str) -> str:
        mapping = {
            "none": "none",
            "qfq": "front_ratio",
            "front": "front_ratio",
            "front_ratio": "front_ratio",
            "hfq": "back_ratio",
            "back": "back_ratio",
            "back_ratio": "back_ratio",
        }
        return mapping.get(adjust.lower(), "front_ratio")

    def _build_miniqmt_mapping(self, df: pd.DataFrame) -> dict[str, str]:
        mapping = self._infer_column_map(df.columns)
        # xtdata.get_market_data_ex 的 reset_index() 通常会得到：
        # index=YYYYMMDD，time=毫秒时间戳。标准化时优先使用 index，
        # 否则 pandas 会把毫秒时间戳当纳秒解析成 1970 年。
        for candidate in ["index", "level_0"]:
            if candidate in df.columns:
                mapping["datetime"] = candidate
                return mapping
        if "datetime" not in mapping:
            if "time" in df.columns:
                mapping["datetime"] = "time"
            elif len(df.columns) > 0:
                mapping["datetime"] = str(df.columns[0])
        return mapping

    def validate_quality(self, df: pd.DataFrame) -> dict[str, Any]:
        warnings: list[str] = []
        passed = True

        if df.empty:
            return {"passed": False, "warnings": ["dataset is empty"], "row_count": 0}

        duplicate_count = int(df.duplicated(subset=["symbol", "datetime"]).sum())
        if duplicate_count:
            warnings.append(f"duplicate rows: {duplicate_count}")

        missing_required = {
            column: int(df[column].isna().sum())
            for column in ["open", "high", "low", "close"]
            if column in df
        }
        for column, count in missing_required.items():
            if count:
                warnings.append(f"missing {column}: {count}")
                passed = False

        invalid_ohlc = df[
            (df["high"] < df[["open", "close", "low"]].max(axis=1))
            | (df["low"] > df[["open", "close", "high"]].min(axis=1))
        ]
        if not invalid_ohlc.empty:
            warnings.append(f"invalid OHLC relationships: {len(invalid_ohlc)}")
            passed = False

        return {
            "passed": passed,
            "warnings": warnings,
            "row_count": int(len(df)),
            "start": str(df["datetime"].min().date()),
            "end": str(df["datetime"].max().date()),
        }

    def save_standard_dataset(
        self,
        df: pd.DataFrame,
        output_dir: str | Path,
        source_used: list[str],
        metadata_extra: dict[str, Any] | None = None,
    ) -> DatasetArtifact:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        dataset_path = output / "ohlcv.parquet"
        metadata_path = output / "dataset_meta.json"
        quality_report_path = output / "data_quality.md"

        quality = self.validate_quality(df)
        df.to_parquet(dataset_path, index=False)

        metadata = {
            "created_at": datetime.now().isoformat(),
            "schema": STANDARD_COLUMNS,
            "row_count": int(len(df)),
            "source_used": source_used,
            "quality": quality,
            **(metadata_extra or {}),
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        quality_report_path.write_text(
            self._format_quality_report(metadata),
            encoding="utf-8",
        )
        return DatasetArtifact(
            dataset_path=dataset_path,
            metadata_path=metadata_path,
            quality_report_path=quality_report_path,
            row_count=int(len(df)),
            source_used=source_used,
            passed=bool(quality["passed"]),
            warnings=list(quality["warnings"]),
        )

    def _infer_column_map(
        self,
        columns: pd.Index,
        mapping: dict[str, str] | None = None,
    ) -> dict[str, str]:
        if mapping:
            return {standard: original for standard, original in mapping.items() if original in columns}

        result: dict[str, str] = {}
        available = list(columns)
        lower_lookup = {str(column).lower(): column for column in available}
        for standard, aliases in DEFAULT_ALIASES.items():
            for alias in aliases:
                if alias in available:
                    result[standard] = alias
                    break
                lower_alias = alias.lower()
                if lower_alias in lower_lookup:
                    result[standard] = lower_lookup[lower_alias]
                    break
        return result

    def _format_quality_report(self, metadata: dict[str, Any]) -> str:
        quality = metadata["quality"]
        lines = [
            "# Data Quality Report",
            "",
            f"- Row count: {metadata['row_count']}",
            f"- Source used: {', '.join(metadata['source_used'])}",
            f"- Passed: {quality['passed']}",
        ]
        if "start" in quality and "end" in quality:
            lines.append(f"- Date range: {quality['start']} to {quality['end']}")
        lines.append("")
        lines.append("## Warnings")
        if quality["warnings"]:
            lines.extend([f"- {warning}" for warning in quality["warnings"]])
        else:
            lines.append("- None")
        lines.append("")
        return "\n".join(lines)
