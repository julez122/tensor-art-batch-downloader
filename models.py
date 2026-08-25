from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MetadataMode(str, Enum):
    NONE = "none"
    SIDECAR = "sidecar"
    EMBED = "embed"


class Artwork(BaseModel):
    image_id: str
    post_id: str | None = None
    detail_url: str
    thumbnail_url: str | None = None
    alt: str | None = None


class ProfileScan(BaseModel):
    scan_id: str
    profile_id: str
    display_name: str
    source_url: str
    default_destination: str
    artworks: list[Artwork] = Field(default_factory=list)

    @property
    def artwork_count(self) -> int:
        return len(self.artworks)

    def api_dict(self) -> dict[str, Any]:
        payload = self.model_dump()
        payload["artworkCount"] = self.artwork_count
        return payload


class ScanRequest(BaseModel):
    profile_url: str = Field(min_length=1, max_length=500)


class DownloadRequest(BaseModel):
    scan_id: str
    image_ids: list[str] = Field(min_length=1)
    destination: str | None = Field(default=None, max_length=1000)
    metadata_mode: MetadataMode = MetadataMode.NONE


class JobItemResult(BaseModel):
    image_id: str
    status: str
    message: str
    filename: str | None = None


class JobSnapshot(BaseModel):
    job_id: str
    scan_id: str
    state: str
    total: int
    completed: int
    skipped: int
    failed: int
    current_image_id: str | None = None
    current_message: str | None = None
    output_folder: str
    metadata_mode: MetadataMode
    results: list[JobItemResult] = Field(default_factory=list)
    cancel_requested: bool = False
