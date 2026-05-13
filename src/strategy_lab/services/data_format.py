from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


def normalize_symbol(symbol: object, reference_symbols: Iterable[str] | None = None) -> str:
    """把资产代码规范成项目内部使用的 `代码.市场` 形式。

    如果提供 reference_symbols，优先按同一 6 位代码匹配参考列表，避免把指数
    `000300.SH` 这类代码误判成深市。
    """

    value = str(symbol).strip().upper()
    if not value:
        return value

    references = [str(item).strip().upper() for item in reference_symbols or [] if str(item).strip()]
    reference_set = set(references)
    if value in reference_set:
        return value

    base = value.split(".", 1)[0]
    for reference in references:
        if reference.split(".", 1)[0] == base:
            return reference

    if "." in value:
        return value

    if len(base) == 6 and base.isdigit():
        if base.startswith(("50", "51", "52", "56", "58", "60", "68", "90")):
            return f"{base}.SH"
        if base.startswith(("15", "16", "18", "30", "12", "13")):
            return f"{base}.SZ"

    return value


def normalize_symbol_series(series: pd.Series, reference_symbols: Iterable[str] | None = None) -> pd.Series:
    return series.map(lambda value: normalize_symbol(value, reference_symbols=reference_symbols))


def normalize_wide_frame(
    frame: pd.DataFrame,
    *,
    reference_symbols: Iterable[str] | None = None,
    datetime_column: str = "datetime",
) -> pd.DataFrame:
    """规范宽表日期索引和资产列。

    支持两种常见输入：
    1. `datetime` 是 index；
    2. `datetime` 是普通列。
    """

    normalized = frame.copy()
    if datetime_column in normalized.columns:
        normalized[datetime_column] = pd.to_datetime(normalized[datetime_column], errors="coerce").dt.normalize()
        normalized = normalized.dropna(subset=[datetime_column]).set_index(datetime_column)
    else:
        normalized.index = pd.to_datetime(normalized.index, errors="coerce").normalize()
        normalized = normalized.loc[normalized.index.notna()].copy()

    normalized.index.name = datetime_column
    normalized.columns = [
        normalize_symbol(column, reference_symbols=reference_symbols)
        for column in normalized.columns
        if str(column).strip().lower() != datetime_column
    ]
    normalized = normalized[~normalized.index.duplicated(keep="last")].sort_index()
    return normalized


def load_wide_parquet(path: Path, *, reference_symbols: Iterable[str] | None = None) -> pd.DataFrame:
    return normalize_wide_frame(pd.read_parquet(path), reference_symbols=reference_symbols)
