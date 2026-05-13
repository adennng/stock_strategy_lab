from __future__ import annotations

from typing import TypedDict


class StrategySearchState(TypedDict, total=False):
    run_id: str
    task_text: str
    experiment_dir: str
    dataset_path: str
    metadata_path: str
    quality_report_path: str
    best_attempt_id: str
    registry_path: str
    status: str

