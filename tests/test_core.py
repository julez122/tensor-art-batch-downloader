from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import piexif
from PIL import Image

from metadata_io import (
    default_destination,
    embed_metadata,
    prepare_metadata,
    sanitize_component,
    write_sidecar,
)
from models import Artwork
from tensor_adapter import canonical_profile_url, dedupe_artworks, normalize_detail_fields


class TensorArtCoreTests(unittest.TestCase):
    def test_canonical_profile_url_accepts_profile_and_posts_page(self) -> None:
        profile_id, url = canonical_profile_url("https://tensor.art/u/1021961773709850720")
        self.assertEqual(profile_id, "1021961773709850720")
        self.assertEqual(url, "https://tensor.art/u/1021961773709850720/posts")

    def test_canonical_profile_url_rejects_non_profile_paths(self) -> None:
        with self.assertRaises(ValueError):
            canonical_profile_url("https://tensor.art/images/1024596470931694233")

    def test_sanitize_component_removes_windows_unsafe_characters(self) -> None:
        self.assertEqual(sanitize_component('a<bad>: name?'), "a_bad__ name_")

    def test_default_destination_uses_tensorart_folder(self) -> None:
        folder = default_destination("test user", "42")
        self.assertEqual(folder.name, "test user_42")
        self.assertEqual(folder.parent.name, "TensorArt")

    def test_dedupe_artworks_keeps_first_image_id(self) -> None:
        first = Artwork(image_id="1", post_id="a", detail_url="https://tensor.art/images/1?post_id=a")
        duplicate = Artwork(image_id="1", post_id="b", detail_url="https://tensor.art/images/1?post_id=b")
        second = Artwork(image_id="2", post_id="c", detail_url="https://tensor.art/images/2?post_id=c")
        result = dedupe_artworks([first, duplicate, second])
        self.assertEqual([item.image_id for item in result], ["1", "2"])
        self.assertEqual(result[0].post_id, "a")

    def test_metadata_normalization_identifies_visible_fields(self) -> None:
        normalized = normalize_detail_fields(
            {
                "Prompt": "positive",
                "Negative Prompt": "negative",
                "Model": "model A",
                "LoRAs": "lora B",
                "Sampler": "Euler",
                "Seed": "123",
                "Size": "1024 x 1536",
                "Steps": "28",
            }
        )
        self.assertEqual(normalized["positive_prompt"], "positive")
        self.assertEqual(normalized["negative_prompt"], "negative")
        self.assertEqual(normalized["model"], "model A")
        self.assertEqual(normalized["loras"], "lora B")
        self.assertEqual(normalized["models"], "model A\nlora B")
        self.assertEqual(normalized["settings"], {"Steps": "28"})

    def test_metadata_normalization_translates_tensor_german_labels(self) -> None:
        normalized = normalize_detail_fields(
            {
                "Negatives Prompt": "negative",
                "Schritte": "25",
                "CFG-Skala": "6",
                "Samen": "123",
                "Größe": "768x1344",
            }
        )

        self.assertEqual(normalized["negative_prompt"], "negative")
        self.assertEqual(normalized["seed"], "123")
        self.assertEqual(normalized["size"], "768x1344")
        self.assertEqual(normalized["settings"], {"Steps": "25", "CFG Scale": "6"})
        self.assertIn("Size", normalized["fields"])
        self.assertNotIn("Größe", normalized["fields"])

    def test_sidecar_and_png_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            image_path = folder / "tensor_9.png"
            Image.new("RGB", (8, 8), color="#123456").save(image_path)
            metadata = prepare_metadata(
                source_url="https://tensor.art/images/9?post_id=8",
                profile_id="7",
                post_id="8",
                image_id="9",
                detail={
                    "positive_prompt": "a small test",
                    "negative_prompt": "a negative test",
                    "model": "Checkpoint A",
                    "loras": "Style LoRA",
                    "fields": {"Sampler": "Euler"},
                },
            )
            sidecar = write_sidecar(image_path, metadata)
            embedded, _ = embed_metadata(image_path, metadata)
            sidecar_text = sidecar.read_text(encoding="utf-8")
            self.assertTrue(sidecar.exists())
            self.assertIn("a small test", sidecar_text)
            self.assertIn("Checkpoint A", sidecar_text)
            self.assertIn("Style LoRA", sidecar_text)
            self.assertGreater(len(sidecar_text.splitlines()), 12)
            self.assertTrue(embedded)
            with Image.open(image_path) as loaded:
                self.assertIn("TensorArtMetadata", loaded.text)

    def test_jpeg_embedding_writes_exif_user_comment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "tensor_12.jpg"
            Image.new("RGB", (8, 8), color="#123456").save(image_path)
            metadata = prepare_metadata(
                source_url="https://tensor.art/images/12?post_id=11",
                profile_id="10",
                post_id="11",
                image_id="12",
                detail={"fields": {"Seed": "123"}},
            )
            embedded, _ = embed_metadata(image_path, metadata)
            self.assertTrue(embedded)
            exif = piexif.load(str(image_path))
            self.assertIn(piexif.ExifIFD.UserComment, exif["Exif"])


if __name__ == "__main__":
    unittest.main()
