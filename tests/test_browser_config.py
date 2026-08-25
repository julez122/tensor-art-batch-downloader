from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tensor_adapter import TensorArtAdapter


class FakeContext:
    async def close(self) -> None:
        return None


class FakeChromium:
    def __init__(self) -> None:
        self.options = None

    async def launch_persistent_context(self, **options):
        self.options = options
        return FakeContext()


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()

    async def stop(self) -> None:
        return None


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


class BrowserConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_browser_uses_resizable_actual_viewport(self) -> None:
        fake_playwright = FakePlaywright()
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "tensor_adapter.async_playwright",
                return_value=FakePlaywrightStarter(fake_playwright),
            ):
                adapter = TensorArtAdapter(Path(temporary) / "profile")
                await adapter._context_or_start()
                options = fake_playwright.chromium.options
                self.assertTrue(options["no_viewport"])
                self.assertIn("--start-maximized", options["args"])
                self.assertNotIn("viewport", options)
                await adapter.close()


if __name__ == "__main__":
    unittest.main()
