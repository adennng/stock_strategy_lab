from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.data_format import load_wide_parquet, normalize_symbol_series
from strategy_lab.services.portfolio_run import PortfolioRunManager


class PortfolioDataSplitRequest(BaseModel):
    portfolio_run_state_path: Path
    panel_ohlcv_path: Path | None = None
    returns_wide_path: Path | None = None
    output_dir: Path | None = None
    split_mode: str = "train-validation-walk-forward"
    train_ratio: float = 0.70
    fold_count: int = 3
    fold_train_ratio: float = 0.60
    fold_validation_ratio: float = 0.20
    min_train_dates: int = 120
    min_validation_dates: int = 40


class PortfolioDataSplitResult(BaseModel):
    portfolio_run_state_path: Path
    output_dir: Path
    manifest_path: Path
    split_mode: str
    source_panel_path: Path
    source_returns_path: Path
    train_panel_path: Path | None = None
    train_returns_path: Path | None = None
    validation_panel_path: Path | None = None
    validation_returns_path: Path | None = None
    walk_forward_dir: Path | None = None
    fold_paths: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class PortfolioDataSplitService:
    """组合层多资产数据切分服务。"""

    VALID_SPLIT_MODES = {"full-only", "train-validation-walk-forward"}

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = PortfolioRunManager(config=self.config)

    def run(self, request: PortfolioDataSplitRequest) -> PortfolioDataSplitResult:
        split_mode = request.split_mode.strip().lower()
        if split_mode not in self.VALID_SPLIT_MODES:
            raise ValueError(f"split_mode 必须是 {sorted(self.VALID_SPLIT_MODES)} 之一。")

        state_path = self._resolve_path(request.portfolio_run_state_path)
        state = self.run_manager.load_state(state_path)
        panel_path = self._resolve_panel_path(request.panel_ohlcv_path, state=state)
        returns_path = self._resolve_returns_path(request.returns_wide_path, state=state)
        output_dir = self._resolve_output_dir(request.output_dir, state=state)
        output_dir.mkdir(parents=True, exist_ok=True)

        reference_symbols = self._reference_symbols_from_state(state)
        panel = self._load_panel(panel_path, reference_symbols=reference_symbols)
        returns = self._load_returns(returns_path, reference_symbols=reference_symbols)
        dates = list(returns.index)
        if not dates:
            raise ValueError("returns_wide 没有有效日期。")

        if split_mode == "full-only":
            manifest = self._build_full_only_manifest(
                panel_path=panel_path,
                returns_path=returns_path,
                output_dir=output_dir,
                dates=dates,
                symbols=list(returns.columns),
                request=request,
            )
            manifest_path = output_dir / "split_manifest.json"
            manifest["manifest_path"] = str(self._relative(manifest_path))
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self._update_run_state(state_path=state_path, state=state, manifest=manifest, manifest_path=manifest_path)
            return PortfolioDataSplitResult(
                portfolio_run_state_path=state_path,
                output_dir=output_dir,
                manifest_path=manifest_path,
                split_mode=split_mode,
                source_panel_path=panel_path,
                source_returns_path=returns_path,
                summary=manifest["summary"],
            )

        if len(dates) < request.min_train_dates + request.min_validation_dates:
            raise ValueError(
                f"有效日期数不足，至少需要 {request.min_train_dates + request.min_validation_dates} 个日期，当前 {len(dates)}。"
            )

        train_dates, validation_dates = self._split_train_validation_dates(dates, train_ratio=request.train_ratio)
        train_panel_path = output_dir / "train_panel_ohlcv.parquet"
        train_returns_path = output_dir / "train_returns_wide.parquet"
        validation_panel_path = output_dir / "validation_panel_ohlcv.parquet"
        validation_returns_path = output_dir / "validation_returns_wide.parquet"
        self._write_slice(panel=panel, returns=returns, dates=train_dates, panel_path=train_panel_path, returns_path=train_returns_path)
        self._write_slice(
            panel=panel,
            returns=returns,
            dates=validation_dates,
            panel_path=validation_panel_path,
            returns_path=validation_returns_path,
        )

        walk_forward_dir = output_dir / "walk_forward"
        walk_forward_dir.mkdir(parents=True, exist_ok=True)
        folds = self._write_walk_forward_folds(
            panel=panel,
            returns=returns,
            dates=dates,
            walk_forward_dir=walk_forward_dir,
            fold_count=request.fold_count,
            fold_train_ratio=request.fold_train_ratio,
            fold_validation_ratio=request.fold_validation_ratio,
            min_train_dates=request.min_train_dates,
            min_validation_dates=request.min_validation_dates,
        )
        manifest = self._build_train_validation_manifest(
            panel_path=panel_path,
            returns_path=returns_path,
            output_dir=output_dir,
            train_panel_path=train_panel_path,
            train_returns_path=train_returns_path,
            validation_panel_path=validation_panel_path,
            validation_returns_path=validation_returns_path,
            walk_forward_dir=walk_forward_dir,
            folds=folds,
            source_dates=len(dates),
            source_symbols=list(returns.columns),
            train_dates=train_dates,
            validation_dates=validation_dates,
            request=request,
        )
        manifest_path = output_dir / "split_manifest.json"
        manifest["manifest_path"] = str(self._relative(manifest_path))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._update_run_state(state_path=state_path, state=state, manifest=manifest, manifest_path=manifest_path)
        return PortfolioDataSplitResult(
            portfolio_run_state_path=state_path,
            output_dir=output_dir,
            manifest_path=manifest_path,
            split_mode=split_mode,
            source_panel_path=panel_path,
            source_returns_path=returns_path,
            train_panel_path=train_panel_path,
            train_returns_path=train_returns_path,
            validation_panel_path=validation_panel_path,
            validation_returns_path=validation_returns_path,
            walk_forward_dir=walk_forward_dir,
            fold_paths=folds,
            summary=manifest["summary"],
        )

    def _split_train_validation_dates(self, dates: list[pd.Timestamp], *, train_ratio: float) -> tuple[list[pd.Timestamp], list[pd.Timestamp]]:
        if not 0.1 <= train_ratio <= 0.9:
            raise ValueError("train_ratio 必须在 0.1 到 0.9 之间。")
        train_end = int(len(dates) * train_ratio)
        train_end = max(1, min(train_end, len(dates) - 1))
        return dates[:train_end], dates[train_end:]

    def _write_walk_forward_folds(
        self,
        *,
        panel: pd.DataFrame,
        returns: pd.DataFrame,
        dates: list[pd.Timestamp],
        walk_forward_dir: Path,
        fold_count: int,
        fold_train_ratio: float,
        fold_validation_ratio: float,
        min_train_dates: int,
        min_validation_dates: int,
    ) -> list[dict[str, Any]]:
        if fold_count <= 0:
            return []
        n = len(dates)
        train_size = max(min_train_dates, int(n * fold_train_ratio))
        validation_size = max(min_validation_dates, int(n * fold_validation_ratio))
        if train_size + validation_size > n:
            train_size = max(min_train_dates, n - validation_size)
        if train_size <= 0 or train_size + validation_size > n:
            raise ValueError("有效日期数不足，无法生成 walk-forward 切分。")

        max_start = n - validation_size
        if fold_count == 1:
            split_points = [train_size]
        else:
            step = max((max_start - train_size) / max(fold_count - 1, 1), 1)
            split_points = sorted({int(round(train_size + i * step)) for i in range(fold_count)})
            split_points = [point for point in split_points if train_size <= point <= max_start]

        folds: list[dict[str, Any]] = []
        for index, validation_start_idx in enumerate(split_points, start=1):
            fold_id = f"fold_{index:03d}"
            fold_dir = walk_forward_dir / fold_id
            fold_dir.mkdir(parents=True, exist_ok=True)
            train_dates = dates[:validation_start_idx]
            validation_dates = dates[validation_start_idx : validation_start_idx + validation_size]
            context_dates = dates[: validation_start_idx + validation_size]
            paths = {
                "context_panel_path": fold_dir / "context_panel_ohlcv.parquet",
                "context_returns_path": fold_dir / "context_returns_wide.parquet",
                "train_panel_path": fold_dir / "train_panel_ohlcv.parquet",
                "train_returns_path": fold_dir / "train_returns_wide.parquet",
                "validation_panel_path": fold_dir / "validation_panel_ohlcv.parquet",
                "validation_returns_path": fold_dir / "validation_returns_wide.parquet",
            }
            self._write_slice(panel=panel, returns=returns, dates=context_dates, panel_path=paths["context_panel_path"], returns_path=paths["context_returns_path"])
            self._write_slice(panel=panel, returns=returns, dates=train_dates, panel_path=paths["train_panel_path"], returns_path=paths["train_returns_path"])
            self._write_slice(
                panel=panel,
                returns=returns,
                dates=validation_dates,
                panel_path=paths["validation_panel_path"],
                returns_path=paths["validation_returns_path"],
            )
            folds.append(
                {
                    "fold_id": fold_id,
                    "context_panel_path": str(self._relative(paths["context_panel_path"])),
                    "context_returns_path": str(self._relative(paths["context_returns_path"])),
                    "train_panel_path": str(self._relative(paths["train_panel_path"])),
                    "train_returns_path": str(self._relative(paths["train_returns_path"])),
                    "validation_panel_path": str(self._relative(paths["validation_panel_path"])),
                    "validation_returns_path": str(self._relative(paths["validation_returns_path"])),
                    "train_start": self._date_text(train_dates, "first"),
                    "train_end": self._date_text(train_dates, "last"),
                    "validation_start": self._date_text(validation_dates, "first"),
                    "validation_end": self._date_text(validation_dates, "last"),
                    "evaluation_start": self._date_text(validation_dates, "first"),
                    "evaluation_end": self._date_text(validation_dates, "last"),
                    "context_start": self._date_text(context_dates, "first"),
                    "context_end": self._date_text(context_dates, "last"),
                    "context_date_count": len(context_dates),
                    "train_date_count": len(train_dates),
                    "validation_date_count": len(validation_dates),
                }
            )
        return folds

    def _write_slice(
        self,
        *,
        panel: pd.DataFrame,
        returns: pd.DataFrame,
        dates: list[pd.Timestamp],
        panel_path: Path,
        returns_path: Path,
    ) -> None:
        date_index = pd.DatetimeIndex(dates)
        sliced_panel = panel.loc[panel["datetime"].isin(date_index)].copy()
        sliced_returns = returns.loc[date_index].copy()
        panel_path.parent.mkdir(parents=True, exist_ok=True)
        sliced_panel.to_parquet(panel_path, index=False)
        sliced_returns.to_parquet(returns_path)

    def _build_full_only_manifest(
        self,
        *,
        panel_path: Path,
        returns_path: Path,
        output_dir: Path,
        dates: list[pd.Timestamp],
        symbols: list[str],
        request: PortfolioDataSplitRequest,
    ) -> dict[str, Any]:
        summary = {
            "split_mode": "full-only",
            "source_date_count": len(dates),
            "symbol_count": len(symbols),
            "symbols": symbols,
            "full_start": self._date_text(dates, "first"),
            "full_end": self._date_text(dates, "last"),
            "walk_forward_fold_count": 0,
        }
        return {
            "created_at": datetime.now().isoformat(),
            "split_mode": "full-only",
            "source_panel_ohlcv_path": str(self._relative(panel_path)),
            "source_returns_wide_path": str(self._relative(returns_path)),
            "full_panel_path": str(self._relative(panel_path)),
            "full_returns_path": str(self._relative(returns_path)),
            "output_dir": str(self._relative(output_dir)),
            "folds": [],
            "parameters": {
                "split_mode": request.split_mode,
            },
            "summary": summary,
        }

    def _build_train_validation_manifest(
        self,
        *,
        panel_path: Path,
        returns_path: Path,
        output_dir: Path,
        train_panel_path: Path,
        train_returns_path: Path,
        validation_panel_path: Path,
        validation_returns_path: Path,
        walk_forward_dir: Path,
        folds: list[dict[str, Any]],
        source_dates: int,
        source_symbols: list[str],
        train_dates: list[pd.Timestamp],
        validation_dates: list[pd.Timestamp],
        request: PortfolioDataSplitRequest,
    ) -> dict[str, Any]:
        summary = {
            "split_mode": "train-validation-walk-forward",
            "source_date_count": source_dates,
            "symbol_count": len(source_symbols),
            "symbols": source_symbols,
            "train_date_count": len(train_dates),
            "validation_date_count": len(validation_dates),
            "walk_forward_fold_count": len(folds),
            "train_start": self._date_text(train_dates, "first"),
            "train_end": self._date_text(train_dates, "last"),
            "validation_start": self._date_text(validation_dates, "first"),
            "validation_end": self._date_text(validation_dates, "last"),
        }
        return {
            "created_at": datetime.now().isoformat(),
            "split_mode": "train-validation-walk-forward",
            "source_panel_ohlcv_path": str(self._relative(panel_path)),
            "source_returns_wide_path": str(self._relative(returns_path)),
            "full_panel_path": str(self._relative(panel_path)),
            "full_returns_path": str(self._relative(returns_path)),
            "output_dir": str(self._relative(output_dir)),
            "train_panel_path": str(self._relative(train_panel_path)),
            "train_returns_path": str(self._relative(train_returns_path)),
            "validation_panel_path": str(self._relative(validation_panel_path)),
            "validation_returns_path": str(self._relative(validation_returns_path)),
            "walk_forward_dir": str(self._relative(walk_forward_dir)),
            "folds": folds,
            "parameters": {
                "split_mode": request.split_mode,
                "train_ratio": request.train_ratio,
                "fold_count": request.fold_count,
                "fold_train_ratio": request.fold_train_ratio,
                "fold_validation_ratio": request.fold_validation_ratio,
                "min_train_dates": request.min_train_dates,
                "min_validation_dates": request.min_validation_dates,
            },
            "summary": summary,
        }

    def _load_panel(self, panel_path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        if not panel_path.exists():
            raise FileNotFoundError(f"panel_ohlcv 文件不存在：{panel_path}")
        df = pd.read_parquet(panel_path)
        if "datetime" not in df.columns or "symbol" not in df.columns:
            raise ValueError("panel_ohlcv 必须包含 datetime 和 symbol 字段。")
        normalized = df.copy()
        normalized["datetime"] = pd.to_datetime(normalized["datetime"], errors="coerce").dt.normalize()
        normalized["symbol"] = normalize_symbol_series(normalized["symbol"], reference_symbols=reference_symbols)
        normalized = normalized.dropna(subset=["datetime", "symbol"])
        return normalized.sort_values(["datetime", "symbol"]).reset_index(drop=True)

    def _load_returns(self, returns_path: Path, *, reference_symbols: list[str]) -> pd.DataFrame:
        if not returns_path.exists():
            raise FileNotFoundError(f"returns_wide 文件不存在：{returns_path}")
        df = load_wide_parquet(returns_path, reference_symbols=reference_symbols)
        if df.empty:
            raise ValueError("returns_wide 为空，无法切分。")
        return df

    def _reference_symbols_from_state(self, state: dict[str, Any]) -> list[str]:
        symbols: list[str] = []
        source_artifacts = state.get("source_artifacts", {}) or {}
        signal_symbols = source_artifacts.get("signals", {}).get("symbols")
        if isinstance(signal_symbols, list):
            symbols.extend(str(item).upper() for item in signal_symbols)
        versions_raw = state.get("versions", {}) or {}
        versions = versions_raw.values() if isinstance(versions_raw, dict) else versions_raw
        for version in versions:
            if isinstance(version, dict) and isinstance(version.get("symbols"), list):
                symbols.extend(str(item).upper() for item in version["symbols"])
        return sorted({item for item in symbols if item})

    def _resolve_panel_path(self, panel_ohlcv_path: Path | None, *, state: dict[str, Any]) -> Path:
        if panel_ohlcv_path:
            return self._resolve_path(panel_ohlcv_path)
        candidate = state.get("data", {}).get("panel_ohlcv")
        if not candidate:
            candidate = state.get("artifacts", {}).get("datasets", {}).get("portfolio_panel", {}).get("panel_ohlcv")
        if not candidate:
            raise ValueError("未传 panel_ohlcv_path，且 portfolio_run_state.json 中没有 data.panel_ohlcv。")
        return self._resolve_path(candidate)

    def _resolve_returns_path(self, returns_wide_path: Path | None, *, state: dict[str, Any]) -> Path:
        if returns_wide_path:
            return self._resolve_path(returns_wide_path)
        candidate = state.get("data", {}).get("returns_wide")
        if not candidate:
            candidate = state.get("artifacts", {}).get("datasets", {}).get("portfolio_panel", {}).get("returns_wide")
        if not candidate:
            raise ValueError("未传 returns_wide_path，且 portfolio_run_state.json 中没有 data.returns_wide。")
        return self._resolve_path(candidate)

    def _resolve_output_dir(self, output_dir: Path | None, *, state: dict[str, Any]) -> Path:
        if output_dir:
            return self._resolve_path(output_dir)
        data_dir = state.get("directories", {}).get("data")
        if not data_dir:
            raise ValueError("portfolio_run_state.json 缺少 directories.data。")
        return self._resolve_path(Path(data_dir) / "splits")

    def _update_run_state(self, *, state_path: Path, state: dict[str, Any], manifest: dict[str, Any], manifest_path: Path) -> None:
        now = datetime.now().isoformat()
        state.setdefault("data", {}).update(
            {
                "status": "split",
                "panel_ohlcv": manifest["source_panel_ohlcv_path"],
                "returns_wide": manifest["source_returns_wide_path"],
                "split_manifest": str(self._relative(manifest_path)),
                "split_mode": manifest["split_mode"],
                "summary": manifest["summary"],
                "error": None,
            }
        )
        state.setdefault("artifacts", {}).setdefault("datasets", {})["portfolio_splits"] = {
            "manifest_path": str(self._relative(manifest_path)),
            "split_mode": manifest["split_mode"],
            "full_panel_path": manifest["full_panel_path"],
            "full_returns_path": manifest["full_returns_path"],
            "train_panel_path": manifest.get("train_panel_path"),
            "train_returns_path": manifest.get("train_returns_path"),
            "validation_panel_path": manifest.get("validation_panel_path"),
            "validation_returns_path": manifest.get("validation_returns_path"),
            "walk_forward_dir": manifest.get("walk_forward_dir"),
            "folds": manifest.get("folds", []),
            "summary": manifest["summary"],
        }
        state.setdefault("events", []).append(
            {
                "timestamp": now,
                "actor": "PortfolioDataSplitService",
                "event": "portfolio_data_split_completed",
                "summary": f"组合层数据切分已生成，模式 {manifest['split_mode']}，walk-forward fold 数 {manifest['summary'].get('walk_forward_fold_count', 0)}。",
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

    def _relative(self, path: str | Path) -> str:
        value = Path(path)
        try:
            return str(value.resolve().relative_to(self.config.root_dir.resolve()))
        except ValueError:
            return str(value)

    @staticmethod
    def _date_text(dates: list[pd.Timestamp], position: str) -> str | None:
        if not dates:
            return None
        value = dates[0 if position == "first" else -1]
        return str(pd.to_datetime(value).date())
