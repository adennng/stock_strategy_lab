from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class DatasetArtifact(BaseModel):
    dataset_path: Path
    metadata_path: Path
    quality_report_path: Path
    row_count: int
    source_used: list[str] = Field(default_factory=list)
    passed: bool
    warnings: list[str] = Field(default_factory=list)

