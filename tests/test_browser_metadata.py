from __future__ import annotations

import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from tensor_adapter import TensorArtAdapter, normalize_detail_fields


class BrowserMetadataExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_model_cards_and_translates_german_field_labels(self) -> None:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                """
                <aside role="complementary" style="height: 80px; overflow-y: auto">
                  <section>
                    <div><h2>Modelle</h2></div>
                    <div>
                      <article><a><h3>Checkpoint A</h3></a><p>100K</p><p>CHECKPOINT</p></article>
                      <article><a><h3>Style LoRA</h3></a><p>5K</p><p>LORA</p></article>
                    </div>
                  </section>
                  <section><div><h2>Prompt</h2></div><p>positive prompt</p></section>
                  <section><div><h2>Negatives Prompt</h2></div><p>negative prompt</p></section>
                  <section><div><h2>Schritte</h2></div><p>25</p></section>
                  <section><div><h2>CFG-Skala</h2></div><p>6</p></section>
                  <section><div><h2>Samen</h2></div><p>123</p></section>
                  <section><div><h2>Größe</h2></div><p>768x1344</p></section>
                </aside>
                """
            )

            adapter = TensorArtAdapter(Path("unused"))
            detail = normalize_detail_fields(await adapter._read_detail_metadata(page))

            self.assertEqual(detail["model"], "Checkpoint A")
            self.assertEqual(detail["loras"], "Style LoRA")
            self.assertEqual(detail["positive_prompt"], "positive prompt")
            self.assertEqual(detail["negative_prompt"], "negative prompt")
            self.assertEqual(detail["seed"], "123")
            self.assertEqual(detail["size"], "768x1344")
            self.assertEqual(detail["settings"], {"Steps": "25", "CFG Scale": "6"})
        finally:
            await browser.close()
            await playwright.stop()
