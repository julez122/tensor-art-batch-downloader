# Tensor.Art Batch Downloader

A Windows-local gallery app for archiving public Tensor.art artworks that you are allowed to download. It scans a profile, lets you choose individual artworks or all visible ones, and uses Tensor.art's normal Download action instead of preview URLs.

## Start it

Double-click "run.bat".

The first launch creates ".venv", installs Python packages, and downloads Playwright Chromium. The app then opens at http://127.0.0.1:8765.

## Use it

1. Paste https://tensor.art/u/<user-id> or its /posts page and choose **Load gallery**.
2. Select individual artworks or use **Select all**.
3. Keep the suggested folder, choose a folder with **Browse**, or type another absolute Windows path.
4. Choose **None**, **Text file**, or **Embed when safe**, then choose **Download selected**.

Each folder receives ".tensor-art-batch.json". It records completed image IDs, so later runs skip them and resume missing or failed artwork.

## Metadata modes

- **None** preserves the downloaded file as received.
- **Text file** writes the prompts, model, LoRAs, and visible generation settings from Tensor.art's post panel to a same-name .txt file beside the artwork.
- **Embed when safe** writes that same metadata to JPEG/WebP EXIF or PNG text metadata. If an original format cannot be safely handled, the app keeps that file unchanged and writes the same-name .txt metadata file instead.

## Deliberate limits

- Only public profile URLs are accepted.
- Only artwork for which Tensor.art exposes its normal Download control is downloaded.
- Private, hidden, removed, inaccessible, or download-disabled items are skipped with their reason.
- The downloader processes one item at a time and spaces Tensor.art requests by at least one second.

## Test

After "run.bat" has installed the environment:

    .venv\Scripts\python.exe -m unittest discover -s tests -v
