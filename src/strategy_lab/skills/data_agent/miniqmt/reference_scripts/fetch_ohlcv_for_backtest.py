from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


THIS_FILE = Path(__file__).resolve()
SRC_DIR = THIS_FILE.parents[5]
PROJECT_ROOT = THIS_FILE.parents[6]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from strategy_lab.config.loader import load_config_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="MiniQMT OHLCV reference script for Backtrader-ready data.")
    parser.add_argument("--symbol", required=True, help="证券代码，例如 600519.SH。")
    parser.add_argument("--period", default="1d", help="周期，例如 1d、1m、5m。")
    parser.add_argument("--start", required=True, help="开始日期，YYYYMMDD 或 YYYY-MM-DD。")
    parser.add_argument("--end", required=True, help="结束日期，YYYYMMDD 或 YYYY-MM-DD。")
    parser.add_argument("--dividend-type", default="front_ratio", help="复权方式：front_ratio、back_ratio、none。")
    parser.add_argument("--output-path", required=True, help="输出 csv/parquet 路径，建议放到 artifacts/data_agent_workspace/data_files。")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载，直接读取本地缓存。")
    args = parser.parse_args()

    load_config_file("qmt")
    from xtquant import xtdata

    xtdata.enable_hello = False
    start = normalize_date(args.start)
    end = normalize_date(args.end)

    if not args.skip_download:
        xtdata.download_history_data(
            args.symbol,
            period=args.period,
            start_time=start,
            end_time=end,
            incrementally=True,
        )

    raw = xtdata.get_market_data_ex(
        [],
        [args.symbol],
        period=args.period,
        start_time=start,
        end_time=end,
        count=-1,
        dividend_type=args.dividend_type,
    )
    df = normalize_ohlcv(raw.get(args.symbol), args.symbol)
    if df.empty:
        print(json.dumps({"ok": False, "error": "未获取到数据。", "symbol": args.symbol}, ensure_ascii=False))
        return 1

    output = resolve_project_path(args.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".csv":
        df.to_csv(output, index=False, encoding="utf-8-sig")
    else:
        if output.suffix.lower() != ".parquet":
            output = output.with_suffix(".parquet")
        df.to_parquet(output, index=False)

    payload = {
        "ok": True,
        "source": "miniqmt",
        "purpose": "backtest_ohlcv",
        "output_path": str(output),
        "row_count": int(len(df)),
        "columns": list(df.columns),
        "date_range": {
            "start": str(df["datetime"].min()),
            "end": str(df["datetime"].max()),
        },
        "backtrader_fields": ["datetime", "open", "high", "low", "close", "volume", "openinterest"],
        "preview_rows": df.head(3).to_dict(orient="records"),
    }
    print(json.dumps(payload, ensure_ascii=False, default=str))
    return 0


def normalize_ohlcv(value, symbol: str) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    df = value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
    if df.empty:
        return df
    if not isinstance(df.index, pd.RangeIndex) or df.index.name:
        df = df.reset_index()
        first_column = str(df.columns[0])
        if first_column not in {"datetime", "date", "time", "trade_date"}:
            df = df.rename(columns={df.columns[0]: "datetime"})
    if "datetime" not in df.columns and "time" in df.columns:
        df = df.rename(columns={"time": "datetime"})
    if "symbol" in df.columns:
        df = df.drop(columns=["symbol"])
    df.insert(0, "symbol", symbol)
    if "openinterest" not in df.columns:
        df["openinterest"] = 0
    if "source" not in df.columns:
        df["source"] = "miniqmt"

    preferred = ["symbol", "datetime", "open", "high", "low", "close", "volume", "openinterest", "amount", "source"]
    ordered = [column for column in preferred if column in df.columns]
    ordered.extend([column for column in df.columns if column not in ordered])
    return df[ordered]


def normalize_date(value: str) -> str:
    return value.strip().replace("-", "")


def resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.root and not candidate.drive:
        candidate = PROJECT_ROOT / str(candidate).lstrip("\\/")
    elif not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError(f"输出路径必须位于项目目录内：{PROJECT_ROOT}") from exc
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
