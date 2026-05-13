from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from strategy_lab.config import AppConfig


@dataclass(frozen=True)
class ExperimentPaths:
    run_id: str
    experiment_dir: Path
    data_dir: Path
    profile_dir: Path
    attempts_dir: Path
    task_md: Path
    task_json: Path
    run_summary_md: Path


class ArtifactManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.artifacts_dir = config.resolve_project_path(config.project.artifacts_dir)
        self.experiments_dir = config.resolve_project_path(config.project.experiments_dir)
        self.strategy_registry_dir = config.resolve_project_path(
            config.project.strategy_registry_dir
        )

    def ensure_base_dirs(self) -> None:
        for directory in [
            self.artifacts_dir,
            self.experiments_dir,
            self.strategy_registry_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

    def new_run_id(self, prefix: str = "run") -> str:
        timezone = ZoneInfo(self.config.project.default_timezone)
        timestamp = datetime.now(timezone).strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{timestamp}"

    def create_experiment(
        self,
        task_text: str,
        run_id: str | None = None,
        metadata: dict | None = None,
    ) -> ExperimentPaths:
        self.ensure_base_dirs()
        actual_run_id = run_id or self.new_run_id()
        experiment_dir = self.experiments_dir / actual_run_id
        data_dir = experiment_dir / "data"
        profile_dir = experiment_dir / "profile"
        attempts_dir = experiment_dir / "attempts"

        for directory in [experiment_dir, data_dir, profile_dir, attempts_dir]:
            directory.mkdir(parents=True, exist_ok=True)

        paths = ExperimentPaths(
            run_id=actual_run_id,
            experiment_dir=experiment_dir,
            data_dir=data_dir,
            profile_dir=profile_dir,
            attempts_dir=attempts_dir,
            task_md=experiment_dir / "task.md",
            task_json=experiment_dir / "task.json",
            run_summary_md=experiment_dir / "run_summary.md",
        )

        paths.task_md.write_text(f"# Task\n\n{task_text}\n", encoding="utf-8")
        payload = {
            "run_id": actual_run_id,
            "task_text": task_text,
            "metadata": metadata or {},
            "created_at": datetime.now(ZoneInfo(self.config.project.default_timezone)).isoformat(),
        }
        paths.task_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return paths

    def create_attempt_dir(self, run_id: str, iteration: int) -> Path:
        attempt_id = f"attempt_{iteration:03d}"
        attempt_dir = self.experiments_dir / run_id / "attempts" / attempt_id
        for child in ["backtest", "reports", "review"]:
            (attempt_dir / child).mkdir(parents=True, exist_ok=True)
        return attempt_dir

