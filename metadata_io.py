from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import piexif
import piexif.helper
from PIL import Image, PngImagePlugin

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_component(value: str, fallback: str = "tensorart") -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", value).strip().rstrip(". ")
    cleaned = re.sub(r"\\s+", " ", cleaned)
    return (cleaned[:80] or fallback).strip()


def default_destination(display_name: str, profile_id: str) -> Path:
    downloads = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"
    return downloads / "TensorArt" / f"{sanitize_component(display_name)}_{profile_id}"


def _metadata_text(metadata: dict[str, Any]) -> str:
    lines = [
        "Tensor.Art artwork metadata",
        "=" * 28,
        f"Source: {metadata.get('source_url', '')}",
        f"Profile ID: {metadata.get('profile_id', '')}",
        f"Post ID: {metadata.get('post_id', '')}",
        f"Image ID: {metadata.get('image_id', '')}",
        f"Retrieved: {metadata.get('retrieved_at', '')}",
        "",
    ]

    labels = [
        ("Positive prompt", metadata.get("positive_prompt")),
        ("Negative prompt", metadata.get("negative_prompt")),
        ("Model", metadata.get("model") or metadata.get("models")),
        ("LoRAs", metadata.get("loras")),
        ("Sampler", metadata.get("sampler")),
        ("Seed", metadata.get("seed")),
        ("Settings", metadata.get("settings")),
        ("Size", metadata.get("size")),
    ]
    for label, value in labels:
        if value:
            lines.append(f"{label}:")
            if label == "Settings" and isinstance(value, dict):
                lines.extend(f"{name}: {setting}" for name, setting in value.items())
            else:
                lines.append(str(value))
            lines.append("")

    summary_fields = {
        "positive prompt",
        "prompt",
        "negative prompt",
        "negatives prompt",
        "model",
        "models",
        "lora",
        "loras",
        "sampler",
        "seed",
        "size",
        "steps",
        "cfg scale",
        "scheduler",
        "clip skip",
        "denoise",
        "strength",
    }
    fields = {
        label: value
        for label, value in (metadata.get("fields") or {}).items()
        if label.casefold().strip() not in summary_fields
    }
    if fields:
        lines.append("All visible fields:")
        for label, value in fields.items():
            lines.extend([f"[{label}]", str(value), ""])

    return "\n".join(lines).strip() + "\n"


def write_sidecar(image_path: Path, metadata: dict[str, Any]) -> Path:
    sidecar = image_path.with_suffix(".txt")
    sidecar.write_text(_metadata_text(metadata), encoding="utf-8")
    return sidecar


def metadata_payload(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))


def _embed_exif(image_path: Path, metadata: dict[str, Any]) -> tuple[bool, str]:
    payload = metadata_payload(metadata).encode("utf-8")
    if len(payload) > 60_000:
        return False, "Metadata is too large for a safe EXIF UserComment."

    description = (
        f"Tensor.Art image {metadata.get('image_id', '')}. "
        f"Full metadata is stored in EXIF UserComment."
    ).encode("utf-8")
    exif_data = {
        "0th": {piexif.ImageIFD.ImageDescription: description},
        "Exif": {
            piexif.ExifIFD.UserComment: piexif.helper.UserComment.dump(
                payload.decode("utf-8"), encoding="unicode"
            )
        },
        "1st": {},
        "thumbnail": None,
    }
    try:
        piexif.insert(piexif.dump(exif_data), str(image_path))
    except Exception as exc:
        return False, f"Could not add EXIF metadata: {exc}"
    return True, "Embedded metadata in EXIF."


def _embed_png(image_path: Path, metadata: dict[str, Any]) -> tuple[bool, str]:
    png_info = PngImagePlugin.PngInfo()
    png_info.add_itxt("TensorArtMetadata", metadata_payload(metadata))
    if metadata.get("positive_prompt"):
        png_info.add_itxt("parameters", str(metadata["positive_prompt"]))

    temporary = None
    try:
        with Image.open(image_path) as image:
            fd, temporary_name = tempfile.mkstemp(
                suffix=".png", prefix="tensorart-metadata-", dir=image_path.parent
            )
            os.close(fd)
            temporary = Path(temporary_name)
            image.save(temporary, format="PNG", pnginfo=png_info)
        temporary.replace(image_path)
    except Exception as exc:
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)
        return False, f"Could not add PNG metadata: {exc}"
    return True, "Embedded metadata in PNG text chunks."


def embed_metadata(image_path: Path, metadata: dict[str, Any]) -> tuple[bool, str]:
    suffix = image_path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".webp"}:
        return _embed_exif(image_path, metadata)
    if suffix == ".png":
        return _embed_png(image_path, metadata)
    return False, f"{suffix or 'This'} format does not support the app's safe metadata writer."


def prepare_metadata(
    *,
    source_url: str,
    profile_id: str,
    post_id: str | None,
    image_id: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    fields = detail.get("fields") or {}
    return {
        "source_url": source_url,
        "profile_id": profile_id,
        "post_id": post_id,
        "image_id": image_id,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "positive_prompt": detail.get("positive_prompt", ""),
        "negative_prompt": detail.get("negative_prompt", ""),
        "model": detail.get("model", ""),
        "loras": detail.get("loras", ""),
        "models": detail.get("models", ""),
        "sampler": detail.get("sampler", ""),
        "seed": detail.get("seed", ""),
        "settings": detail.get("settings", ""),
        "size": detail.get("size", ""),
        "fields": fields,
    }
