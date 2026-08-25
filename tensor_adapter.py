from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from models import Artwork

PROFILE_PATH = re.compile(r"^/u/(?P<profile_id>\d+)(?:/posts)?/?$")
IMAGE_PATH = re.compile(r"^/images/(?P<image_id>\d+)$")
SCROLL_WAIT_MS = 1_000
SCROLL_IDLE_ROUNDS = 12
DETAIL_SCROLL_WAIT_MS = 300
DETAIL_BOTTOM_ROUNDS = 2


class TensorArtError(RuntimeError):
    pass


class DownloadUnavailable(TensorArtError):
    pass


def canonical_profile_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"tensor.art", "www.tensor.art"}:
        raise ValueError("Use a public Tensor.art profile URL.")
    match = PROFILE_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("Use https://tensor.art/u/<user-id> or its /posts page.")
    profile_id = match.group("profile_id")
    return profile_id, f"https://tensor.art/u/{profile_id}/posts"


def normalize_detail_fields(fields: dict[str, str]) -> dict[str, Any]:
    def normalized_label(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[:/_&-]+", " ", value.casefold())).strip()

    aliases = {
        "positiver prompt": "prompt",
        "negatives prompt": "negative prompt",
        "negativer prompt": "negative prompt",
        "negativ prompt": "negative prompt",
        "modelle": "models",
        "schritte": "steps",
        "cfg skala": "cfg scale",
        "samen": "seed",
        "grösse": "size",
        "auflösung": "size",
    }
    display_labels = {
        "prompt": "Positive prompt",
        "positive prompt": "Positive prompt",
        "negative prompt": "Negative prompt",
        "model": "Model",
        "models": "Models",
        "loras": "LoRAs",
        "lora": "LoRAs",
        "sampler": "Sampler",
        "seed": "Seed",
        "size": "Size",
        "steps": "Steps",
        "cfg scale": "CFG Scale",
        "scheduler": "Scheduler",
        "clip skip": "Clip Skip",
        "denoise": "Denoise",
        "strength": "Strength",
    }

    canonical_fields: dict[str, str] = {}
    visible_fields: dict[str, str] = {}
    for label, value in fields.items():
        if not value:
            continue
        canonical_label = aliases.get(normalized_label(label), normalized_label(label))
        canonical_fields[canonical_label] = value
        visible_fields[display_labels.get(canonical_label, label.strip())] = value

    def first(*names: str) -> str:
        for name in names:
            if name in canonical_fields:
                return canonical_fields[name]
        return ""

    settings_labels = {
        "steps",
        "cfg scale",
        "cfg",
        "scheduler",
        "clip skip",
        "denoise",
        "strength",
    }
    model = first("model", "models", "modelle", "base model", "checkpoint")
    loras = first("lora", "loras", "lora model", "lora models")
    combined_models = first("models loras", "model lora", "models and loras")
    models = "\n".join(value for value in (model, loras) if value) or combined_models
    settings = {
        display_labels[key]: value
        for key, value in canonical_fields.items()
        if key in settings_labels
    }
    return {
        "fields": visible_fields,
        "positive_prompt": first("prompt", "positive prompt", "positiver prompt"),
        "negative_prompt": first(
            "negative prompt", "negatives prompt", "negative", "negativer prompt", "negativ prompt"
        ),
        "model": model,
        "loras": loras,
        "models": models,
        "sampler": first("sampler"),
        "seed": first("seed"),
        "size": first("size", "größe", "resolution", "dimensions"),
        "settings": settings,
    }


def dedupe_artworks(artworks: list[Artwork]) -> list[Artwork]:
    seen: set[str] = set()
    result: list[Artwork] = []
    for artwork in artworks:
        if artwork.image_id not in seen:
            result.append(artwork)
            seen.add(artwork.image_id)
    return result


class TensorArtAdapter:
    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self._playwright = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def _context_or_start(self) -> BrowserContext:
        if self._context is None:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                accept_downloads=True,
                no_viewport=True,
                args=["--start-maximized"],
            )
        return self._context

    async def _new_page(self) -> Page:
        context = await self._context_or_start()
        return await context.new_page()

    async def _respect_cadence(self) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_request
        if elapsed < 1:
            await asyncio.sleep(1 - elapsed)
        self._last_request = loop.time()

    async def scan_profile(self, profile_url: str) -> tuple[str, str, list[Artwork]]:
        profile_id, canonical_url = canonical_profile_url(profile_url)
        async with self._lock:
            await self._respect_cadence()
            page = await self._new_page()
            try:
                await page.goto(canonical_url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(1_500)
                display_name = await self._read_display_name(page, profile_id)
                artworks = await self._collect_artworks(page)
                return profile_id, display_name, artworks
            finally:
                await page.close()

    async def _read_display_name(self, page: Page, fallback: str) -> str:
        heading = page.locator("h1")
        if await heading.count():
            try:
                value = (await heading.first.inner_text()).strip()
                if value:
                    return value
            except Exception:
                pass
        return fallback

    async def _collect_artworks(self, page: Page) -> list[Artwork]:
        collected: dict[str, Artwork] = {}
        idle_rounds = 0

        while True:
            records = await page.locator('a[href*="/images/"]').evaluate_all(
                """elements => elements.map(link => {
                    const image = link.querySelector('img');
                    return {
                        href: link.href,
                        thumbnail: image ? (image.currentSrc || image.src || null) : null,
                        alt: image ? (image.alt || null) : null
                    };
                })"""
            )
            before = len(collected)
            for record in records:
                parsed = urlparse(record["href"])
                match = IMAGE_PATH.fullmatch(parsed.path)
                if not match:
                    continue
                image_id = match.group("image_id")
                post_id = parse_qs(parsed.query).get("post_id", [None])[0]
                collected[image_id] = Artwork(
                    image_id=image_id,
                    post_id=post_id,
                    detail_url=record["href"],
                    thumbnail_url=record["thumbnail"],
                    alt=record["alt"],
                )

            scroll_state = await page.evaluate(
                """() => {
                    const canScroll = (element) => {
                        const style = window.getComputedStyle(element);
                        return (
                            (style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            element.scrollHeight > element.clientHeight + 4
                        );
                    };
                    const imageLinks = Array.from(document.querySelectorAll('a[href*="/images/"]'));
                    const candidates = new Map();
                    for (const link of imageLinks) {
                        for (let element = link.parentElement; element; element = element.parentElement) {
                            if (canScroll(element)) {
                                candidates.set(element, (candidates.get(element) || 0) + 1);
                            }
                        }
                    }
                    const target = Array.from(candidates.entries())
                        .sort((left, right) => {
                            const countDifference = right[1] - left[1];
                            return countDifference || (right[0].scrollHeight - right[0].clientHeight) -
                                (left[0].scrollHeight - left[0].clientHeight);
                        })[0]?.[0] || document.scrollingElement;
                    const distance = Math.max(window.innerHeight * 0.9, 700);
                    target.scrollBy(0, distance);
                    return {
                        atBottom: target.scrollTop + target.clientHeight >= target.scrollHeight - 4,
                    };
                }"""
            )
            await page.wait_for_timeout(SCROLL_WAIT_MS)
            if len(collected) == before and scroll_state["atBottom"]:
                idle_rounds += 1
            else:
                idle_rounds = 0
            if idle_rounds >= SCROLL_IDLE_ROUNDS:
                break

        return dedupe_artworks(list(collected.values()))

    async def download_artwork(
        self,
        *,
        artwork: Artwork,
        destination: Path,
        staging_path: Path,
    ) -> tuple[Path, dict[str, Any]]:
        async with self._lock:
            await self._respect_cadence()
            page = await self._new_page()
            try:
                await page.goto(artwork.detail_url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(1_000)
                detail = await self._read_detail_metadata(page)
                button = page.locator('button[title="Ctrl + S"]')
                count = await button.count()
                if count != 1:
                    button = page.locator('button:has(iconpark-icon[icon-id="download"])')
                    count = await button.count()
                if count != 1:
                    raise DownloadUnavailable(
                        "Tensor.art's normal Download control was unavailable for this image."
                    )

                try:
                    async with page.expect_download(timeout=30_000) as download_info:
                        await button.click()
                    download = await download_info.value
                except Exception as exc:
                    raise DownloadUnavailable(
                        f"Tensor.art did not provide a normal download for this image: {exc}"
                    ) from exc

                suggested = download.suggested_filename or f"tensor_{artwork.image_id}.bin"
                extension = Path(suggested).suffix.lower() or ".bin"
                final_path = destination / f"tensor_{artwork.image_id}{extension}"
                await download.save_as(str(staging_path))
                return final_path, normalize_detail_fields(detail)
            finally:
                await page.close()

    async def _read_detail_metadata(self, page: Page) -> dict[str, str]:
        fields: dict[str, str] = {}
        bottom_rounds = 0

        while True:
            fields.update(await self._read_visible_detail_fields(page))
            scroll_state = await page.evaluate(
                """() => {
                    const roots = Array.from(document.querySelectorAll(
                        'aside, [role="complementary"], [role="dialog"], [role="tabpanel"], article'
                    ));
                    const metadataWords = /prompt|negative|model|lora|sampler|seed|steps|cfg|scheduler/i;
                    const candidates = Array.from(document.querySelectorAll('*'))
                        .filter(element => roots.some(root => root.contains(element)))
                        .filter(element => element.scrollHeight > element.clientHeight + 4)
                        .sort((left, right) => {
                            const leftScore = (metadataWords.test(left.innerText || '') ? 1_000_000 : 0) +
                                left.scrollHeight - left.clientHeight;
                            const rightScore = (metadataWords.test(right.innerText || '') ? 1_000_000 : 0) +
                                right.scrollHeight - right.clientHeight;
                            return rightScore - leftScore;
                        });
                    const target = candidates[0];
                    if (!target) return { atBottom: true };
                    target.scrollBy(0, Math.max(target.clientHeight * 0.8, 420));
                    return {
                        atBottom: target.scrollTop + target.clientHeight >= target.scrollHeight - 4,
                    };
                }"""
            )
            await page.wait_for_timeout(DETAIL_SCROLL_WAIT_MS)
            if scroll_state["atBottom"]:
                bottom_rounds += 1
            else:
                bottom_rounds = 0
            if bottom_rounds >= DETAIL_BOTTOM_ROUNDS:
                fields.update(await self._read_visible_detail_fields(page))
                return fields

    async def _read_visible_detail_fields(self, page: Page) -> dict[str, str]:
        return await page.locator("section").evaluate_all(
            """sections => {
                const fields = {};
                const aliases = {
                    'positiver prompt': 'prompt',
                    'negatives prompt': 'negative prompt',
                    'negativer prompt': 'negative prompt',
                    'negativ prompt': 'negative prompt',
                    'modelle': 'models',
                    'schritte': 'steps',
                    'cfg skala': 'cfg scale',
                    'samen': 'seed',
                    'größe': 'size',
                    'auflösung': 'size'
                };
                const normalized = value => (value || '')
                    .toLocaleLowerCase()
                    .replace(/[:/_&-]+/g, ' ')
                    .replace(/\s+/g, ' ')
                    .trim();
                const textOf = element => (element?.innerText || '').trim();
                for (const section of sections) {
                    const heading = section.querySelector('h2');
                    if (!heading) continue;
                    const label = textOf(heading);
                    if (!label) continue;
                    const field = aliases[normalized(label)] || normalized(label);

                    if (field === 'models') {
                        const models = [];
                        const loras = [];
                        for (const card of section.querySelectorAll('article')) {
                            const name = textOf(card.querySelector('h3')) || textOf(card.querySelector('a'));
                            if (!name) continue;
                            const type = Array.from(card.querySelectorAll('p'))
                                .map(textOf)
                                .find(value => /^(checkpoint|lora|vae|embedding|controlnet)\\b/i.test(value)) || '';
                            if (/^lora\\b/i.test(type)) loras.push(name);
                            else models.push(name);
                        }
                        if (models.length) fields.model = models.join('\\n');
                        if (loras.length) fields.loras = loras.join('\\n');
                        continue;
                    }

                    const value = Array.from(section.children)
                        .filter(element => !element.contains(heading))
                        .map(textOf)
                        .filter(Boolean)
                        .join('\\n')
                        .trim();
                    if (value) fields[label] = value;
                }
                return fields;
            }"""
        )

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
