from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.budget_run import BudgetRunManager


STANDARD_PANEL_COLUMNS = [
    "symbol",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "pctchange",
    "return",
    "source",
]


class BudgetDataPanelRequest(BaseModel):
    budget_run_state_path: Path
    output_dir: Path | None = None
    allow_pending_missing_data: bool = False
    include_supplemental: bool = True
    min_rows_per_asset: int = 20


class BudgetDataPanelResult(BaseModel):
    budget_run_state_path: Path
    output_dir: Path
    panel_ohlcv_path: Path
    returns_wide_path: Path
    manifest_path: Path
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class BudgetDataPanelService:
    """预算层多资产行情面板服务。"""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = BudgetRunManager(config=self.config)

    def run(self, request: BudgetDataPanelRequest) -> BudgetDataPanelResult:
        state_path = self._resolve_path(request.budget_run_state_path)
        state = self.run_manager.load_state(state_path)
        data_panel_state = state.get("data_panel", {}) or {}
        missing_data = list(data_panel_state.get("missing_data") or [])
        if missing_data and not request.allow_pending_missing_data:
            raise ValueError(
                "budget_run_state.json 中仍存在 data_panel.missing_data。"
                "请先调用 DataAgent 补齐数据，或显式传入 --allow-pending-missing-data 生成临时面板。"
            )

        output_dir = self._resolve_output_dir(request.output_dir, state=state)
        output_dir.mkdir(parents=True, exist_ok=True)

        signal_manifest_path = self._resolve_signal_manifest_path(state)
        signal_manifest = json.loads(signal_manifest_path.read_text(encoding="utf-8"))
        records = list(signal_manifest.get("records") or [])
        if not records:
            raise ValueError(f"signal_artifacts_manifest.json 中没有可用资产记录：{signal_manifest_path}")

        start_date, end_date = self._resolve_date_range(state)
        frames: list[pd.DataFrame] = []
        asset_summaries: list[dict[str, Any]] = []
        warnings: list[str] = []
        for record in records:
            frame, summary = self._load_asset_panel(
                record=record,
                start_date=start_date,
                end_date=end_date,
                include_supplemental=request.include_supplemental,
                min_rows_per_asset=request.min_rows_per_asset,
            )
            asset_summaries.append(summary)
            warnings.extend(summary.get("warnings") or [])
            if frame is not None and not frame.empty:
                frames.append(frame)

        if not frames:
            raise ValueError("没有任何资产生成有效行情数据，无法创建预算层数据面板。")

        panel = pd.concat(frames, ignore_index=True)
        panel = self._finalize_panel(panel, start_date=start_date, end_date=end_date)
        returns_wide = self._build_returns_wide(panel)

        panel_path = output_dir / "panel_ohlcv.parquet"
        returns_path = output_dir / "returns_wide.parquet"
        manifest_path = output_dir / "data_panel_manifest.json"
        panel.to_parquet(panel_path, index=False)
        returns_wide.to_parquet(returns_path)

        status = "success" if not warnings and not missing_data else "partial"
        manifest = self._build_manifest(
            state=state,
            signal_manifest_path=signal_manifest_path,
            panel_path=panel_path,
            returns_path=returns_path,
            manifest_path=manifest_path,
            panel=panel,
            returns_wide=returns_wide,
            asset_summaries=asset_summaries,
            warnings=warnings,
            status=status,
            request=request,
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._update_run_state(
            state_path=state_path,
            state=state,
            manifest=manifest,
            panel_path=panel_path,
            returns_path=returns_path,
            manifest_path=manifest_path,
            status=status,
            warnings=warnings,
        )

        return BudgetDataPanelResult(
            budget_run_state_path=state_path,
            output_dir=output_dir,
            panel_ohlcv_path=panel_path,
            returns_wide_path=returns_path,
            manifest_path=manifest_path,
            status=status,
            summary=manifest["summary"],
            warnings=warnings,
        )

    def _load_asset_panel(
        self,
        *,
        record: dict[str, Any],
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
        include_supplemental: bool,
        min_rows_per_asset: int,
    ) -> tuple[pd.DataFrame | None, dict[str, Any]]:
        symbol = str(record.get("symbol") or "").strip().upper()
        summary: dict[str, Any] = {
            "symbol": symbol,
            "primary_dataset": record.get("primary_dataset"),
            "supplemental_files": [],
            "warnings": [],
            "status": "pending",
        }
        if not symbol:
            summary["status"] = "failed"
            summary["warnings"].append("资产记录缺少 symbol。")
            return None, summary

        sources: list[pd.DataFrame] = []
        primary = record.get("primary_dataset")
        if primary:
            primary_path = self._resolve_path(primary)
            if primary_path.exists():
                sources.append(self._read_market_file(primary_path, symbol=symbol, source_label="primary"))
            else:
                summary["warnings"].append(f"primary_dataset 文件不存在：{primary}")
        else:
            summary["warnings"].append("资产记录缺少 primary_dataset。")

        if include_supplemental:
            supplemental_dir = record.get("required_data_output_dir")
            if supplemental_dir:
                resolved_dir = self._resolve_path(supplemental_dir)
                if resolved_dir.exists():
                    for path in sorted(resolved_dir.glob("*")):
                        if path.suffix.lower() in {".parquet", ".csv", ".txt", ".json", ".jsonl"}:
                            sources.append(self._read_market_file(path, symbol=symbol, source_label="supplemental"))
                            summary["supplemental_files"].append(str(self._relative(path)))

        if not sources:
            summary["status"] = "failed"
            summary["warnings"].append("没有可读取的 primary 或 supplemental 行情文件。")
            return None, summary

        df = pd.concat(sources, ignore_index=True)
        df = self._normalize_asset_df(df, symbol=symbol)
        df = df[(df["datetime"] >= start_date) & (df["datetime"] <= end_date)].copy()
        df = df.sort_values("datetime").drop_duplicates(subset=["symbol", "datetime"], keep="last")
        df["return"] = df["close"].pct_change()
        if "pctchange" not in df.columns or df["pctchange"].isna().all():
            df["pctchange"] = df["return"] * 100.0
        df["return"] = df["return"].fillna(df["pctchange"] / 100.0)
        df.loc[df.groupby("symbol").head(1).index, "return"] = 0.0

        row_count = int(len(df))
        summary.update(
            {
                "row_count": row_count,
                "start_date": self._date_text(df, "first"),
                "end_date": self._date_text(df, "last"),
                "columns": list(df.columns),
            }
        )
        if row_count < min_rows_per_asset:
            summary["warnings"].append(f"有效行数低于 min_rows_per_asset={min_rows_per_asset}。")
        if df.empty:
            summary["status"] = "failed"
            summary["warnings"].append("按预算层 task.data_range 截取后数据为空。")
            return None, summary
        actual_start = pd.to_datetime(df["datetime"].min())
        actual_end = pd.to_datetime(df["datetime"].max())
        if actual_start > start_date + pd.Timedelta(days=10):
            summary["warnings"].append(
                f"{symbol} 实际开始日期 {self._date_text(df, 'first')} 晚于预算层开始日期 {start_date.date()}，该资产前段不可用。"
            )
        if actual_end < end_date - pd.Timedelta(days=10):
            summary["warnings"].append(
                f"{symbol} 实际结束日期 {self._date_text(df, 'last')} 早于预算层结束日期 {end_date.date()}，该资产后段不可用。"
            )
        summary["status"] = "success" if not summary["warnings"] else "partial"
        return df[STANDARD_PANEL_COLUMNS].copy(), summary

    def _read_market_file(self, path: Path, *, symbol: str, source_label: str) -> pd.DataFrame:
        suffix = path.suffix.lower()
        if suffix == ".parquet":
            df = pd.read_parquet(path)
        elif suffix in {".csv", ".txt"}:
            df = pd.read_csv(path)
        elif suffix in {".json", ".jsonl"}:
            df = pd.read_json(path, lines=suffix == ".jsonl")
        else:
            raise ValueError(f"不支持的行情文件格式：{path}")
        if "symbol" not in df.columns:
            df["symbol"] = symbol
        df["_panel_source_label"] = source_label
        return df

    def _normalize_asset_df(self, df: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
        required = ["datetime", "open", "high", "low", "close"]
        missing = [column for column in required if column not in df.columns]
        if missing:
            raise ValueError(f"{symbol} 行情数据缺少必要字段：{missing}")
        normalized = df.copy()
        normalized["symbol"] = normalized["symbol"].fillna(symbol).astype(str).str.upper()
        normalized = normalized.loc[normalized["symbol"] == symbol].copy()
        normalized["datetime"] = self._parse_datetime(normalized["datetime"]).dt.normalize()
        for column in ["open", "high", "low", "close", "volume", "pctchange"]:
            if column not in normalized.columns:
                normalized[column] = pd.NA
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized["source"] = normalized.get("source", normalized.get("_panel_source_label", "unknown"))
        normalized["source"] = normalized["source"].fillna(normalized.get("_panel_source_label", "unknown"))
        normalized = normalized.dropna(subset=["datetime", "open", "high", "low", "close"])
        return normalized

    def _finalize_panel(self, panel: pd.DataFrame, *, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
        panel = panel.copy()
        panel = panel[(panel["datetime"] >= start_date) & (panel["datetime"] <= end_date)]
        panel = panel.sort_values(["datetime", "symbol"]).reset_index(drop=True)
        panel["datetime"] = pd.to_datetime(panel["datetime"]).dt.normalize()
        return panel[STANDARD_PANEL_COLUMNS]

    def _build_returns_wide(self, panel: pd.DataFrame) -> pd.DataFrame:
        returns = panel.pivot(index="datetime", columns="symbol", values="return").sort_index()
        returns.index.name = "datetime"
        return returns

    def _build_manifest(
        self,
        *,
        state: dict[str, Any],
        signal_manifest_path: Path,
        panel_path: Path,
        returns_path: Path,
        manifest_path: Path,
        panel: pd.DataFrame,
        returns_wide: pd.DataFrame,
        asset_summaries: list[dict[str, Any]],
        warnings: list[str],
        status: str,
        request: BudgetDataPanelRequest,
    ) -> dict[str, Any]:
        row_count_by_symbol = panel.groupby("symbol").size().astype(int).to_dict()
        coverage_start = panel.groupby("symbol")["datetime"].min().dt.date.astype(str).to_dict()
        coverage_end = panel.groupby("symbol")["datetime"].max().dt.date.astype(str).to_dict()
        summary = {
            "status": status,
            "symbol_count": int(panel["symbol"].nunique()),
            "row_count": int(len(panel)),
            "date_count": int(len(returns_wide)),
            "start_date": str(panel["datetime"].min().date()),
            "end_date": str(panel["datetime"].max().date()),
            "columns": STANDARD_PANEL_COLUMNS,
            "returns_wide_columns": list(returns_wide.columns),
            "row_count_by_symbol": row_count_by_symbol,
            "coverage_start_by_symbol": coverage_start,
            "coverage_end_by_symbol": coverage_end,
            "warning_count": len(warnings),
        }
        return {
            "created_at": datetime.now().isoformat(),
            "budget_run_id": state.get("budget_run_id"),
            "status": status,
            "task_data_range": state.get("task", {}).get("data_range", {}),
            "signal_manifest_path": str(self._relative(signal_manifest_path)),
            "panel_ohlcv_path": str(self._relative(panel_path)),
            "returns_wide_path": str(self._relative(returns_path)),
            "manifest_path": str(self._relative(manifest_path)),
            "parameters": {
                "allow_pending_missing_data": request.allow_pending_missing_data,
                "include_supplemental": request.include_supplemental,
                "min_rows_per_asset": request.min_rows_per_asset,
            },
            "summary": summary,
            "assets": asset_summaries,
            "warnings": warnings,
        }

    def _update_run_state(
        self,
        *,
        state_path: Path,
        state: dict[str, Any],
        manifest: dict[str, Any],
        panel_path: Path,
        returns_path: Path,
        manifest_path: Path,
        status: str,
        warnings: list[str],
    ) -> None:
        now = datetime.now().isoformat()
        state.setdefault("data_panel", {}).update(
            {
                "status": status,
                "panel_ohlcv": str(self._relative(panel_path)),
                "returns_wide": str(self._relative(returns_path)),
                "manifest_path": str(self._relative(manifest_path)),
                "start_date": manifest["summary"]["start_date"],
                "end_date": manifest["summary"]["end_date"],
                "symbols": manifest["summary"]["returns_wide_columns"],
                "summary": manifest["summary"],
                "error": "; ".join(warnings) if warnings else None,
            }
        )
        state.setdefault("artifacts", {}).setdefault("datasets", {})["budget_panel"] = {
            "panel_ohlcv": str(self._relative(panel_path)),
            "returns_wide": str(self._relative(returns_path)),
            "manifest_path": str(self._relative(manifest_path)),
            "summary": manifest["summary"],
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "BudgetDataPanelService",
                "event": "budget_data_panel_completed" if status != "failed" else "budget_data_panel_failed",
                "summary": f"预算层多资产行情面板已生成，状态：{status}，资产数：{manifest['summary']['symbol_count']}。",
                "manifest_path": str(self._relative(manifest_path)),
            }
        )
        state["updated_at"] = now
        self.run_manager.save_state(state_path, state)

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        data_dir = state.get("directories", {}).get("data")
        if not data_dir:
            raise ValueError("budget_run_state.json 缺少 directories.data。")
        return self._resolve_path(data_dir)

    def _resolve_signal_manifest_path(self, state: dict[str, Any]) -> Path:
        candidate = state.get("signal_artifacts", {}).get("manifest_path")
        if not candidate:
            candidate = state.get("artifacts", {}).get("run_reports", {}).get("signal_artifacts_manifest", {}).get("path")
        if not candidate:
            raise ValueError("budget_run_state.json 中找不到 signal_artifacts_manifest 路径。")
        path = self._resolve_path(candidate)
        if not path.exists():
            raise FileNotFoundError(f"signal_artifacts_manifest.json 不存在：{path}")
        return path

    def _resolve_date_range(self, state: dict[str, Any]) -> tuple[pd.Timestamp, pd.Timestamp]:
        data_range = state.get("task", {}).get("data_range", {})
        start = data_range.get("start")
        end = data_range.get("end")
        if not start or not end:
            raise ValueError("budget_run_state.json 的 task.data_range 缺少 start 或 end。")
        return pd.to_datetime(start).normalize(), pd.to_datetime(end).normalize()

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

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: str | Path) -> str:
        value = Path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)

    @staticmethod
    def _date_text(df: pd.DataFrame, position: str) -> str | None:
        if df.empty or "datetime" not in df.columns:
            return None
        value = df["datetime"].iloc[0 if position == "first" else -1]
        return str(pd.to_datetime(value).date())
