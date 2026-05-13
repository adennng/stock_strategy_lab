from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from strategy_lab.config import AppConfig, load_app_config
from strategy_lab.services.signal_run import SignalRunManager


class DataSplitRequest(BaseModel):
    data_path: Path | None = None
    run_state_path: Path | None = None
    output_dir: Path | None = None
    train_ratio: float = 0.70
    fold_count: int = 3
    fold_train_ratio: float = 0.60
    fold_validation_ratio: float = 0.20
    min_train_rows: int = 60
    min_validation_rows: int = 20


class DataSplitResult(BaseModel):
    data_path: Path
    output_dir: Path
    train_path: Path
    validation_path: Path
    walk_forward_dir: Path
    manifest_path: Path
    fold_paths: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any]


class DataSplitService:
    """信号层时间序列数据切分服务。

    只按时间顺序生成 train、validation 和 walk-forward fold，不随机打乱数据。
    """

    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_app_config()
        self.run_manager = SignalRunManager(config=self.config)

    def run(self, request: DataSplitRequest) -> DataSplitResult:
        run_state = self._load_run_state(request.run_state_path)
        data_path = self._resolve_data_path(request.data_path, run_state=run_state)
        output_dir = self._resolve_output_dir(request.output_dir, run_state=run_state)
        output_dir.mkdir(parents=True, exist_ok=True)

        df = self._load_market_data(data_path)
        if len(df) < request.min_train_rows + request.min_validation_rows:
            raise ValueError(
                f"数据行数不足，至少需要 {request.min_train_rows + request.min_validation_rows} 行，当前 {len(df)} 行。"
            )

        train_df, validation_df = self._split_train_validation(df, train_ratio=request.train_ratio)
        train_path = output_dir / "train.parquet"
        validation_path = output_dir / "validation.parquet"
        train_df.to_parquet(train_path, index=False)
        validation_df.to_parquet(validation_path, index=False)

        walk_forward_dir = output_dir / "walk_forward"
        walk_forward_dir.mkdir(parents=True, exist_ok=True)
        fold_paths = self._write_walk_forward_folds(
            df=df,
            walk_forward_dir=walk_forward_dir,
            fold_count=request.fold_count,
            fold_train_ratio=request.fold_train_ratio,
            fold_validation_ratio=request.fold_validation_ratio,
            min_train_rows=request.min_train_rows,
            min_validation_rows=request.min_validation_rows,
        )
        manifest = self._build_manifest(
            data_path=data_path,
            output_dir=output_dir,
            train_path=train_path,
            validation_path=validation_path,
            walk_forward_dir=walk_forward_dir,
            fold_paths=fold_paths,
            source_rows=len(df),
            train_df=train_df,
            validation_df=validation_df,
            request=request,
        )
        manifest_path = output_dir / "split_manifest.json"
        manifest["manifest_path"] = str(self._relative(manifest_path))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        if request.run_state_path:
            self._update_run_state(
                run_state_path=request.run_state_path,
                manifest=manifest,
                manifest_path=manifest_path,
            )

        return DataSplitResult(
            data_path=data_path,
            output_dir=output_dir,
            train_path=train_path,
            validation_path=validation_path,
            walk_forward_dir=walk_forward_dir,
            manifest_path=manifest_path,
            fold_paths=fold_paths,
            summary=manifest["summary"],
        )

    def _split_train_validation(self, df: pd.DataFrame, train_ratio: float) -> tuple[pd.DataFrame, pd.DataFrame]:
        if not 0.1 <= train_ratio <= 0.9:
            raise ValueError("train_ratio 必须在 0.1 到 0.9 之间。")
        train_end = int(len(df) * train_ratio)
        train_end = max(1, min(train_end, len(df) - 1))
        return df.iloc[:train_end].copy(), df.iloc[train_end:].copy()

    def _write_walk_forward_folds(
        self,
        *,
        df: pd.DataFrame,
        walk_forward_dir: Path,
        fold_count: int,
        fold_train_ratio: float,
        fold_validation_ratio: float,
        min_train_rows: int,
        min_validation_rows: int,
    ) -> list[dict[str, Any]]:
        if fold_count <= 0:
            return []
        n = len(df)
        train_size = max(min_train_rows, int(n * fold_train_ratio))
        validation_size = max(min_validation_rows, int(n * fold_validation_ratio))
        if train_size + validation_size > n:
            train_size = max(min_train_rows, n - validation_size)
        if train_size <= 0 or train_size + validation_size > n:
            raise ValueError("数据行数不足，无法生成 walk-forward 切分。")

        max_start = n - validation_size
        if fold_count == 1:
            split_points = [train_size]
        else:
            step = max((max_start - train_size) / max(fold_count - 1, 1), 1)
            split_points = sorted({int(round(train_size + i * step)) for i in range(fold_count)})
            split_points = [point for point in split_points if train_size <= point <= max_start]

        folds: list[dict[str, Any]] = []
        for index, split_start in enumerate(split_points, start=1):
            fold_id = f"fold_{index:03d}"
            fold_dir = walk_forward_dir / fold_id
            fold_dir.mkdir(parents=True, exist_ok=True)
            train_df = df.iloc[:split_start].copy()
            validation_df = df.iloc[split_start : split_start + validation_size].copy()
            context_df = df.iloc[: split_start + validation_size].copy()
            train_path = fold_dir / "train.parquet"
            validation_path = fold_dir / "validation.parquet"
            context_path = fold_dir / "context.parquet"
            train_df.to_parquet(train_path, index=False)
            validation_df.to_parquet(validation_path, index=False)
            context_df.to_parquet(context_path, index=False)
            folds.append(
                {
                    "fold_id": fold_id,
                    "context_path": str(self._relative(context_path)),
                    "train_path": str(self._relative(train_path)),
                    "validation_path": str(self._relative(validation_path)),
                    "train_start": self._date_text(train_df, "first"),
                    "train_end": self._date_text(train_df, "last"),
                    "validation_start": self._date_text(validation_df, "first"),
                    "validation_end": self._date_text(validation_df, "last"),
                    "evaluation_start": self._date_text(validation_df, "first"),
                    "evaluation_end": self._date_text(validation_df, "last"),
                    "context_rows": int(len(context_df)),
                    "train_rows": int(len(train_df)),
                    "validation_rows": int(len(validation_df)),
                }
            )
        return folds

    def _build_manifest(
        self,
        *,
        data_path: Path,
        output_dir: Path,
        train_path: Path,
        validation_path: Path,
        walk_forward_dir: Path,
        fold_paths: list[dict[str, Any]],
        source_rows: int,
        train_df: pd.DataFrame,
        validation_df: pd.DataFrame,
        request: DataSplitRequest,
    ) -> dict[str, Any]:
        summary = {
            "source_rows": source_rows,
            "train_rows": int(len(train_df)),
            "validation_rows": int(len(validation_df)),
            "walk_forward_fold_count": int(len(fold_paths)),
            "train_start": self._date_text(train_df, "first"),
            "train_end": self._date_text(train_df, "last"),
            "validation_start": self._date_text(validation_df, "first"),
            "validation_end": self._date_text(validation_df, "last"),
            "columns": list(train_df.columns),
        }
        return {
            "created_at": datetime.now().isoformat(),
            "source_data_path": str(self._relative(data_path)),
            "output_dir": str(self._relative(output_dir)),
            "train_path": str(self._relative(train_path)),
            "validation_path": str(self._relative(validation_path)),
            "walk_forward_dir": str(self._relative(walk_forward_dir)),
            "folds": fold_paths,
            "parameters": {
                "train_ratio": request.train_ratio,
                "fold_count": request.fold_count,
                "fold_train_ratio": request.fold_train_ratio,
                "fold_validation_ratio": request.fold_validation_ratio,
                "min_train_rows": request.min_train_rows,
                "min_validation_rows": request.min_validation_rows,
            },
            "summary": summary,
        }

    def _load_market_data(self, data_path: Path) -> pd.DataFrame:
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
            raise ValueError(f"暂不支持的数据格式：{suffix}")

        if "datetime" not in df.columns:
            raise ValueError("数据文件必须包含 datetime 字段。")
        normalized = df.copy()
        normalized["datetime"] = pd.to_datetime(normalized["datetime"], errors="coerce")
        normalized = normalized.dropna(subset=["datetime"])
        normalized = normalized.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
        normalized = normalized.reset_index(drop=True)
        if normalized.empty:
            raise ValueError("清洗后的数据为空。")
        return normalized

    def _load_run_state(self, run_state_path: Path | None) -> dict[str, Any] | None:
        if not run_state_path:
            return None
        path = self._resolve_path(run_state_path)
        if not path.exists():
            raise FileNotFoundError(f"run_state.json 不存在：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _resolve_data_path(self, data_path: Path | None, *, run_state: dict[str, Any] | None) -> Path:
        if data_path is not None:
            return self._resolve_path(data_path)
        primary_dataset = (run_state or {}).get("steps", {}).get("data_acquisition", {}).get("primary_dataset")
        if not primary_dataset:
            raise ValueError("未传 data_path，且 run_state.json 中没有 steps.data_acquisition.primary_dataset。")
        return self._resolve_path(primary_dataset)

    def _resolve_output_dir(self, output_dir: Path | None, *, run_state: dict[str, Any] | None) -> Path:
        if output_dir is not None:
            return self._resolve_path(output_dir)
        data_dir = (run_state or {}).get("directories", {}).get("data")
        if data_dir:
            return self._resolve_path(Path(data_dir) / "splits")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.config.root_dir / "artifacts" / "data_splits" / timestamp

    def _update_run_state(self, run_state_path: Path, *, manifest: dict[str, Any], manifest_path: Path) -> None:
        state = self.run_manager.load_state(run_state_path)
        datasets = state.setdefault("artifacts", {}).setdefault("datasets", {})
        datasets["splits"] = {
            "manifest_path": str(self._relative(manifest_path)),
            "train_path": manifest["train_path"],
            "validation_path": manifest["validation_path"],
            "walk_forward_dir": manifest["walk_forward_dir"],
            "folds": manifest["folds"],
            "summary": manifest["summary"],
        }
        state.setdefault("events", []).append(
            {
                "timestamp": datetime.now().isoformat(),
                "actor": "DataSplitService",
                "event": "data_split_completed",
                "summary": f"生成训练/验证/walk-forward 数据切分，共 {manifest['summary']['walk_forward_fold_count']} 个 fold。",
                "manifest_path": str(self._relative(manifest_path)),
            }
        )
        state["updated_at"] = datetime.now().isoformat()
        self.run_manager.save_state(run_state_path, state)

    def _resolve_path(self, path: str | Path) -> Path:
        value = Path(path)
        if value.is_absolute():
            return value
        return self.config.root_dir / value

    def _relative(self, path: Path) -> Path:
        try:
            return path.resolve().relative_to(self.config.root_dir.resolve())
        except ValueError:
            return path

    def _date_text(self, df: pd.DataFrame, position: str) -> str | None:
        if df.empty or "datetime" not in df.columns:
            return None
        value = df["datetime"].iloc[0 if position == "first" else -1]
        return str(pd.to_datetime(value).date())
