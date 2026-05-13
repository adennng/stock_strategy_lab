from __future__ import annotations

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

from strategy_lab.config.loader import load_config_file


def main() -> int:
    # Settings
    symbol = "159995.SZ"
    period = "1d"
    start = "20150101"
    end = "20241231"
    dividend_type = "front_ratio"
    
    # Output paths
    data_dir = PROJECT_ROOT / "artifacts" / "signal_runs" / "signal_159995_SZ_20260511_035719" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = data_dir / "signal_159995_SZ_20260511_035719_primary_dataset.parquet"
    manifest_path = data_dir / "signal_159995_SZ_20260511_035719_dataset_manifest.json"

    load_config_file("qmt")
    from xtquant import xtdata
    xtdata.enable_hello = False

    # Download data
    print("Downloading history data...")
    xtdata.download_history_data(
        symbol,
        period=period,
        start_time=start,
        end_time=end,
        incrementally=True,
    )

    # Get market data
    raw = xtdata.get_market_data_ex(
        [],
        [symbol],
        period=period,
        start_time=start,
        end_time=end,
        count=-1,
        dividend_type=dividend_type,
    )

    df_raw = raw.get(symbol)
    if df_raw is None:
        print(json.dumps({"ok": False, "error": f"未获取到 {symbol} 的数据。", "symbol": symbol}, ensure_ascii=False))
        return 1

    df = df_raw.copy() if isinstance(df_raw, pd.DataFrame) else pd.DataFrame(df_raw)
    if df.empty:
        print(json.dumps({"ok": False, "error": f"{symbol} 数据为空。", "symbol": symbol}, ensure_ascii=False))
        return 1

    # Normalize: reset index -> datetime column
    if not isinstance(df.index, pd.RangeIndex) or df.index.name:
        df = df.reset_index()
        first_col = str(df.columns[0])
        if first_col not in {"datetime", "date", "time", "trade_date"}:
            df = df.rename(columns={df.columns[0]: "datetime"})
    if "datetime" not in df.columns and "time" in df.columns:
        df = df.rename(columns={"time": "datetime"})

    # Ensure datetime is proper
    df["datetime"] = pd.to_datetime(df["datetime"])

    # Drop existing symbol column if present, add our own
    if "symbol" in df.columns:
        df = df.drop(columns=["symbol"])
    df.insert(0, "symbol", symbol)

    # Add pctchange
    if "close" in df.columns:
        df["pctchange"] = df["close"].pct_change() * 100.0
        df["pctchange"] = df["pctchange"].round(6)
    else:
        df["pctchange"] = 0.0

    # Add source
    df["source"] = "miniqmt"

    # Compute actual date range
    actual_start = df["datetime"].min()
    actual_end = df["datetime"].max()

    # Order columns
    preferred = ["symbol", "datetime", "open", "high", "low", "close", "volume", "pctchange", "source"]
    ordered = [c for c in preferred if c in df.columns]
    ordered.extend([c for c in df.columns if c not in ordered])
    df = df[ordered]

    # Save parquet
    df.to_parquet(output_path, index=False)

    # Build manifest
    manifest = {
        "dataset_id": "signal_159995_SZ_20260511_035719_primary",
        "source": "miniqmt",
        "symbol": symbol,
        "asset_type": "etf",
        "frequency": "1d",
        "dividend_type": dividend_type,
        "start_date": str(actual_start.date()),
        "end_date": str(actual_end.date()),
        "requested_start": "2015-01-01",
        "requested_end": "2024-12-31",
        "row_count": int(len(df)),
        "columns": list(df.columns),
        "file_format": "parquet",
        "file_path": str(output_path.relative_to(PROJECT_ROOT)),
        "created_at": datetime.now().isoformat(),
        "quality_checks": {
            "missing_pct": {c: round(df[c].isna().mean() * 100, 2) for c in df.columns},
            "has_duplicates": bool(df.duplicated(subset=["datetime"]).any()),
            "actual_start_note": (
                f"实际数据起始日期为 {actual_start.date()}，晚于请求的 2015-01-01。"
                f"该ETF（半导体ETF）上市日期为 2020-01-20，故2015-2020年1月无数据。"
                if str(actual_start.date()) > "2015-01-02"
                else "数据从请求起始日期开始。"
            ),
        },
        "notes": (
            "159995.SZ（国证半导体芯片ETF）于2020-01-20在深交所上市。"
            "数据实际从上市日期附近开始，请求的2015-01-01至上市前无交易日数据。"
            "pctchange 基于 close 的前复权价格计算，第一条记录的 pctchange 为 NaN。"
        ),
    }

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

    print(json.dumps({
        "ok": True,
        "output_path": str(output_path),
        "manifest_path": str(manifest_path),
        "row_count": int(len(df)),
        "columns": list(df.columns),
        "date_range": {
            "start": str(actual_start.date()),
            "end": str(actual_end.date()),
        },
        "note": f"实际起始日期：{actual_start.date()}，ETF于2020-01-20上市",
    }, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
