from __future__ import annotations

import unittest
from pathlib import Path

from app import serialize_sse_event
from models import JobSnapshot, MetadataMode
from tensor_adapter import TensorArtAdapter


class FakeGalleryLocator:
    def __init__(self, page: "FakeGalleryPage") -> None:
        self.page = page

    async def evaluate_all(self, _: str) -> list[dict[str, str | None]]:
        return self.page.records[min(self.page.scrolls, len(self.page.records) - 1)]


class FakeGalleryPage:
    def __init__(self) -> None:
        image_one = {
            "href": "https://tensor.art/images/1?post_id=11",
            "thumbnail": "https://cdn.example.test/1.webp",
            "alt": "First image",
        }
        image_two = {
            "href": "https://tensor.art/images/2?post_id=12",
            "thumbnail": "https://cdn.example.test/2.webp",
            "alt": "Second image",
        }
        self.records = [[image_one]] * 12 + [[image_one, image_two]]
        self.scrolls = 0
        self.waits = 0
        self.scroll_scripts: list[str] = []

    def locator(self, selector: str) -> FakeGalleryLocator:
        if selector != 'a[href*="/images/"]':
            raise AssertionError(f"Unexpected selector: {selector}")
        return FakeGalleryLocator(self)

    async def evaluate(self, script: str) -> dict[str, bool]:
        self.scroll_scripts.append(script)
        self.scrolls += 1
        return {"atBottom": True}

    async def wait_for_timeout(self, _: int) -> None:
        self.waits += 1


class FakeMetadataLocator:
    def __init__(self, page: "FakeMetadataPage") -> None:
        self.page = page

    async def evaluate_all(self, _: str) -> dict[str, str]:
        return self.page.fields[min(self.page.scrolls, len(self.page.fields) - 1)]


class FakeMetadataPage:
    def __init__(self) -> None:
        self.fields = [
            {"Prompt": "positive prompt"},
            {"Negative Prompt": "negative prompt"},
            {"Model": "Checkpoint A", "LoRAs": "Style LoRA"},
        ]
        self.scrolls = 0
        self.scroll_scripts: list[str] = []

    def locator(self, _: str) -> FakeMetadataLocator:
        return FakeMetadataLocator(self)

    async def evaluate(self, script: str) -> dict[str, bool]:
        self.scroll_scripts.append(script)
        if self.scrolls < len(self.fields) - 1:
            self.scrolls += 1
        return {"atBottom": self.scrolls == len(self.fields) - 1}

    async def wait_for_timeout(self, _: int) -> None:
        return None


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_gallery_scan_waits_through_a_long_lazy_load_pause(self) -> None:
        page = FakeGalleryPage()
        adapter = TensorArtAdapter(Path("unused"))

        artworks = await adapter._collect_artworks(page)  # type: ignore[arg-type]

        self.assertEqual([artwork.image_id for artwork in artworks], ["1", "2"])
        self.assertGreaterEqual(page.waits, 13)
        self.assertIn("document.scrollingElement", page.scroll_scripts[0])
        self.assertIn("imageLinks", page.scroll_scripts[0])

    async def test_sse_event_uses_real_event_delimiters(self) -> None:
        job = JobSnapshot(
            job_id="job",
            scan_id="scan",
            state="running",
            total=2,
            completed=1,
            skipped=0,
            failed=0,
            output_folder="C:\\downloads",
            metadata_mode=MetadataMode.NONE,
        )

        event = serialize_sse_event(job)

        self.assertTrue(event.startswith("data: {"))
        self.assertTrue(event.endswith("\n\n"))
        self.assertNotIn("\\\\n", event)

    async def test_metadata_reader_collects_fields_while_scrolling_the_panel(self) -> None:
        page = FakeMetadataPage()
        adapter = TensorArtAdapter(Path("unused"))

        fields = await adapter._read_detail_metadata(page)  # type: ignore[arg-type]

        self.assertEqual(fields["Prompt"], "positive prompt")
        self.assertEqual(fields["Negative Prompt"], "negative prompt")
        self.assertEqual(fields["Model"], "Checkpoint A")
        self.assertEqual(fields["LoRAs"], "Style LoRA")
        self.assertIn('[role="complementary"]', page.scroll_scripts[0])
