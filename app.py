from __future__ import annotations

import asyncio
import json
import os
import uuid
import webbrowser
from asyncio import Lock
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from metadata_io import default_destination, embed_metadata, prepare_metadata, write_sidecar
from models import (
    Artwork,
    DownloadRequest,
    JobItemResult,
    JobSnapshot,
    MetadataMode,
    ProfileScan,
    ScanRequest,
)
from tensor_adapter import DownloadUnavailable, TensorArtAdapter, canonical_profile_url

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = PROJECT_ROOT / "static"
STATE_ROOT = PROJECT_ROOT / ".state"
MANIFEST_NAME = ".tensor-art-batch.json"


def serialize_sse_event(job: JobSnapshot) -> str:
    return f"data: {job.model_dump_json()}\n\n"


def choose_folder() -> str | None:
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="Choose Tensor.Art download folder")
        root.destroy()
        return selected or None
    except Exception:
        return None


def _read_manifest(destination: Path) -> dict[str, Any]:
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.exists():
        return {"version": 1, "items": {}}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
            return loaded
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "items": {}}


def _write_manifest(destination: Path, manifest: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / MANIFEST_NAME
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


class JobManager:
    def __init__(self, adapter: TensorArtAdapter) -> None:
        self.adapter = adapter
        self.scans: dict[str, ProfileScan] = {}
        self.jobs: dict[str, JobSnapshot] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = Lock()

    async def create_scan(self, profile_url: str) -> ProfileScan:
        profile_id, canonical_url = canonical_profile_url(profile_url)
        returned_profile_id, display_name, artworks = await self.adapter.scan_profile(canonical_url)
        scan = ProfileScan(
            scan_id=uuid.uuid4().hex,
            profile_id=returned_profile_id or profile_id,
            display_name=display_name,
            source_url=canonical_url,
            default_destination=str(default_destination(display_name, returned_profile_id or profile_id)),
            artworks=artworks,
        )
        async with self._lock:
            self.scans[scan.scan_id] = scan
        return scan

    async def start_job(self, request: DownloadRequest) -> JobSnapshot:
        async with self._lock:
            scan = self.scans.get(request.scan_id)
            if scan is None:
                raise KeyError("The gallery scan is no longer available. Scan the profile again.")

            selected_ids = set(request.image_ids)
            selected = [item for item in scan.artworks if item.image_id in selected_ids]
            if not selected:
                raise ValueError("Choose at least one artwork from the loaded gallery.")

            destination_value = request.destination or scan.default_destination
            destination = Path(destination_value).expanduser()
            if not destination.is_absolute():
                raise ValueError("Choose an absolute destination folder.")

            job = JobSnapshot(
                job_id=uuid.uuid4().hex,
                scan_id=scan.scan_id,
                state="queued",
                total=len(selected),
                completed=0,
                skipped=0,
                failed=0,
                output_folder=str(destination),
                metadata_mode=request.metadata_mode,
            )
            self.jobs[job.job_id] = job
            self._tasks[job.job_id] = asyncio.create_task(
                self._run_job(job.job_id, scan, selected, destination)
            )
            return job

    async def get_job(self, job_id: str) -> JobSnapshot | None:
        async with self._lock:
            return self.jobs.get(job_id)

    async def cancel_job(self, job_id: str) -> JobSnapshot | None:
        async with self._lock:
            job = self.jobs.get(job_id)
            if job is not None and job.state in {"queued", "running"}:
                job.cancel_requested = True
                job.current_message = "Cancellation requested; finishing the current artwork."
            return job

    async def _mutate_job(self, job_id: str, **changes: Any) -> JobSnapshot:
        async with self._lock:
            job = self.jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            return job

    async def _append_result(self, job_id: str, result: JobItemResult) -> JobSnapshot:
        async with self._lock:
            job = self.jobs[job_id]
            job.results.append(result)
            if result.status == "completed":
                job.completed += 1
            elif result.status == "skipped":
                job.skipped += 1
            elif result.status == "failed":
                job.failed += 1
            return job

    async def _run_job(
        self,
        job_id: str,
        scan: ProfileScan,
        artworks: list[Artwork],
        destination: Path,
    ) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        manifest = _read_manifest(destination)
        manifest["profile_id"] = scan.profile_id
        manifest["profile_url"] = scan.source_url
        manifest.setdefault("items", {})
        await self._mutate_job(job_id, state="running", current_message="Preparing download queue.")

        for artwork in artworks:
            job = await self.get_job(job_id)
            if job is None or job.cancel_requested:
                await self._mutate_job(
                    job_id,
                    state="cancelled",
                    current_message="Cancelled. Completed downloads remain in the destination.",
                    current_image_id=None,
                )
                _write_manifest(destination, manifest)
                return

            existing = manifest["items"].get(artwork.image_id, {})
            existing_name = existing.get("filename")
            if existing.get("status") == "completed" and existing_name:
                existing_path = destination / existing_name
                if existing_path.exists():
                    await self._append_result(
                        job_id,
                        JobItemResult(
                            image_id=artwork.image_id,
                            status="skipped",
                            message="Already completed in this destination.",
                            filename=existing_name,
                        ),
                    )
                    continue

            existing_files = [
                path
                for path in destination.glob(f"tensor_{artwork.image_id}.*")
                if path.suffix.lower() not in {".txt", ".part"}
            ]
            if existing_files:
                existing_path = existing_files[0]
                manifest["items"][artwork.image_id] = {
                    "status": "completed",
                    "filename": existing_path.name,
                    "detail_url": artwork.detail_url,
                    "post_id": artwork.post_id,
                    "metadata_mode": "unknown",
                }
                _write_manifest(destination, manifest)
                await self._append_result(
                    job_id,
                    JobItemResult(
                        image_id=artwork.image_id,
                        status="skipped",
                        message="A matching image ID already exists in this destination.",
                        filename=existing_path.name,
                    ),
                )
                continue

            await self._mutate_job(
                job_id,
                current_image_id=artwork.image_id,
                current_message=f"Downloading image {artwork.image_id}.",
            )

            staging = destination / f".tensor_{artwork.image_id}.part"
            try:
                final_path: Path | None = None
                detail: dict[str, Any] | None = None
                last_error: Exception | None = None
                for attempt in range(3):
                    try:
                        final_path, detail = await self.adapter.download_artwork(
                            artwork=artwork,
                            destination=destination,
                            staging_path=staging,
                        )
                        break
                    except DownloadUnavailable as exc:
                        if "normal Download control was unavailable" in str(exc):
                            raise
                        last_error = exc
                    except Exception as exc:
                        last_error = exc

                    if attempt < 2:
                        delay = 2 * (attempt + 1)
                        await self._mutate_job(
                            job_id,
                            current_message=(
                                f"Retrying image {artwork.image_id} in {delay} seconds "
                                f"({attempt + 1} of 3 attempts)."
                            ),
                        )
                        await asyncio.sleep(delay)
                if final_path is None or detail is None:
                    raise last_error or RuntimeError("Download did not return a file.")

                final_path.parent.mkdir(parents=True, exist_ok=True)
                staging.replace(final_path)
                metadata = prepare_metadata(
                    source_url=artwork.detail_url,
                    profile_id=scan.profile_id,
                    post_id=artwork.post_id,
                    image_id=artwork.image_id,
                    detail=detail,
                )

                message = "Downloaded."
                if job.metadata_mode == MetadataMode.SIDECAR:
                    write_sidecar(final_path, metadata)
                    message = "Downloaded with metadata text file."
                elif job.metadata_mode == MetadataMode.EMBED:
                    embedded, embed_message = embed_metadata(final_path, metadata)
                    if not embedded:
                        write_sidecar(final_path, metadata)
                        message = f"Downloaded; {embed_message} Wrote a metadata text file instead."
                    else:
                        message = f"Downloaded; {embed_message}"

                manifest["items"][artwork.image_id] = {
                    "status": "completed",
                    "filename": final_path.name,
                    "detail_url": artwork.detail_url,
                    "post_id": artwork.post_id,
                    "metadata_mode": job.metadata_mode.value,
                }
                _write_manifest(destination, manifest)
                await self._append_result(
                    job_id,
                    JobItemResult(
                        image_id=artwork.image_id,
                        status="completed",
                        message=message,
                        filename=final_path.name,
                    ),
                )
            except DownloadUnavailable as exc:
                manifest["items"][artwork.image_id] = {
                    "status": "skipped",
                    "detail_url": artwork.detail_url,
                    "message": str(exc),
                }
                _write_manifest(destination, manifest)
                await self._append_result(
                    job_id,
                    JobItemResult(
                        image_id=artwork.image_id,
                        status="skipped",
                        message=str(exc),
                    ),
                )
            except Exception as exc:
                manifest["items"][artwork.image_id] = {
                    "status": "failed",
                    "detail_url": artwork.detail_url,
                    "message": str(exc),
                }
                _write_manifest(destination, manifest)
                await self._append_result(
                    job_id,
                    JobItemResult(
                        image_id=artwork.image_id,
                        status="failed",
                        message=str(exc),
                    ),
                )
            finally:
                if staging.exists():
                    staging.unlink(missing_ok=True)

        job = await self.get_job(job_id)
        if job is not None:
            summary = f"Finished: {job.completed} downloaded, {job.skipped} skipped, {job.failed} failed."
            await self._mutate_job(
                job_id,
                state="completed",
                current_message=summary,
                current_image_id=None,
            )

    async def close(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.adapter.close()


manager = JobManager(TensorArtAdapter(STATE_ROOT / "tensorart-browser-profile"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await manager.close()


app = FastAPI(title="Tensor.Art Batch Downloader", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@app.post("/api/profile/scan")
async def scan_profile(request: ScanRequest) -> dict[str, Any]:
    try:
        scan = await manager.create_scan(request.profile_url)
        return scan.api_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not load this Tensor.art profile: {exc}") from exc


@app.post("/api/folder/choose")
async def folder_choose() -> dict[str, str | None]:
    selected = await asyncio.to_thread(choose_folder)
    return {"path": selected}


@app.post("/api/jobs")
async def create_job(request: DownloadRequest) -> dict[str, Any]:
    try:
        job = await manager.start_job(request)
        return job.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = await manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Download job not found.")
    return job.model_dump(mode="json")


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    job = await manager.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Download job not found.")
    return job.model_dump(mode="json")


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    if await manager.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Download job not found.")

    async def events() -> AsyncIterator[str]:
        while True:
            job = await manager.get_job(job_id)
            if job is None:
                return
            yield serialize_sse_event(job)
            if job.state in {"completed", "cancelled"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    webbrowser.open("http://127.0.0.1:8765")
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
