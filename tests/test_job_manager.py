from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app import JobManager
from models import Artwork, DownloadRequest, MetadataMode, ProfileScan


class FakeAdapter:
    def __init__(self) -> None:
        self.download_calls = 0

    async def download_artwork(self, *, artwork, destination, staging_path):
        self.download_calls += 1
        Image.new("RGB", (8, 8), color="#123456").save(staging_path, format="PNG")
        return (
            destination / f"tensor_{artwork.image_id}.png",
            {
                "positive_prompt": "test prompt",
                "negative_prompt": "test negative",
                "fields": {"Sampler": "Euler", "Seed": "123"},
            },
        )

    async def close(self) -> None:
        return None


class JobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_embed_job_writes_manifest_and_later_resumes_by_skipping(self) -> None:
        adapter = FakeAdapter()
        manager = JobManager(adapter)
        artwork = Artwork(
            image_id="99",
            post_id="98",
            detail_url="https://tensor.art/images/99?post_id=98",
        )
        scan = ProfileScan(
            scan_id="scan",
            profile_id="97",
            display_name="tester",
            source_url="https://tensor.art/u/97/posts",
            default_destination="C:\\unused",
            artworks=[artwork],
        )
        manager.scans[scan.scan_id] = scan

        with tempfile.TemporaryDirectory() as temporary:
            request = DownloadRequest(
                scan_id=scan.scan_id,
                image_ids=["99"],
                destination=temporary,
                metadata_mode=MetadataMode.EMBED,
            )
            first = await manager.start_job(request)
            await manager._tasks[first.job_id]
            completed = await manager.get_job(first.job_id)
            self.assertEqual(completed.state, "completed")
            self.assertEqual(completed.completed, 1)
            self.assertTrue((Path(temporary) / "tensor_99.png").exists())
            self.assertTrue((Path(temporary) / ".tensor-art-batch.json").exists())

            second = await manager.start_job(request)
            await manager._tasks[second.job_id]
            resumed = await manager.get_job(second.job_id)
            self.assertEqual(resumed.skipped, 1)
            self.assertEqual(adapter.download_calls, 1)

        await manager.close()


if __name__ == "__main__":
    unittest.main()
