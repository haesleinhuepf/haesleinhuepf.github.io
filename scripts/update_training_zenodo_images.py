#!/usr/bin/env python3
"""Create preview images for Zenodo-backed training entries.

The script scans data/training.json for Zenodo links, downloads a PDF from each
record via the Zenodo API, renders the first page to images/<record_id>.png, and
adds that image URL to the corresponding entry.

Requires:
    pip install pymupdf

Authentication:
    Set ZENODO_API_KEY in the environment. Public records usually also work
    without a token, but the token is sent when available.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ZENODO_RECORD_RE = re.compile(r"zenodo\.org/(?:record|records)/(\d+)", re.IGNORECASE)
ZENODO_DOI_RE = re.compile(r"(?:doi\.org/)?10\.5281/zenodo\.(\d+)", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate PNG thumbnails from Zenodo PDFs in data/training.json."
    )
    parser.add_argument(
        "--training-json",
        default="data/training.json",
        type=Path,
        help="Path to the training JSON file.",
    )
    parser.add_argument(
        "--images-dir",
        default="images",
        type=Path,
        help="Directory where generated PNG files are stored.",
    )
    parser.add_argument(
        "--image-url-prefix",
        default="images",
        help="URL/path prefix stored in the JSON image property.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate PNGs even when the output file already exists.",
    )
    parser.add_argument(
        "--overwrite-images",
        action="store_true",
        help="Update entries that already have an image property.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without downloading or writing files.",
    )
    parser.add_argument(
        "--dpi",
        default=160,
        type=int,
        help="Rendering resolution for the first PDF page.",
    )
    return parser.parse_args()


def zenodo_record_id(url: str) -> str | None:
    for regex in (ZENODO_RECORD_RE, ZENODO_DOI_RE):
        match = regex.search(url)
        if match:
            return match.group(1)
    return None


def entry_record_id(entry: dict[str, Any]) -> str | None:
    for link in entry.get("links", []):
        if not isinstance(link, dict):
            continue

        url = link.get("url")
        if not isinstance(url, str):
            continue

        record_id = zenodo_record_id(url)
        if record_id:
            return record_id

    return None


def request_json(url: str, api_key: str | None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=request_headers(api_key))
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def request_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def zenodo_record(record_id: str, api_key: str | None) -> dict[str, Any]:
    return request_json(f"https://zenodo.org/api/records/{record_id}", api_key)


def file_name(file_entry: dict[str, Any]) -> str:
    value = file_entry.get("key") or file_entry.get("filename") or file_entry.get("name")
    return str(value or "")


def safe_file_name(name: str) -> str:
    return Path(name).name or "zenodo-download.pdf"


def download_url(file_entry: dict[str, Any]) -> str | None:
    links = file_entry.get("links")
    if isinstance(links, dict):
        for key in ("download", "self"):
            value = links.get(key)
            if isinstance(value, str):
                return value
    return None


def pdf_score(file_entry: dict[str, Any], position: int) -> tuple[int, int]:
    name = file_name(file_entry).lower()
    score = 0
    if "preview" in name:
        score += 100
    if "thumbnail" in name:
        score += 30
    if "slides" in name or "presentation" in name:
        score += 10
    return score, -position


def is_pdf_file(file_entry: dict[str, Any]) -> bool:
    name = file_name(file_entry).lower()
    file_type = str(file_entry.get("type") or file_entry.get("mimetype") or "").lower()
    return name.endswith(".pdf") or file_type == "pdf" or file_type == "application/pdf"


def choose_pdf_file(record: dict[str, Any]) -> dict[str, Any] | None:
    files = record.get("files")
    if not isinstance(files, list):
        return None

    pdf_files = [
        file_entry
        for file_entry in files
        if isinstance(file_entry, dict) and is_pdf_file(file_entry)
    ]
    if not pdf_files:
        return None

    return max(enumerate(pdf_files), key=lambda item: pdf_score(item[1], item[0]))[1]


def download_file(url: str, target: Path, api_key: str | None) -> None:
    request = urllib.request.Request(url, headers=request_headers(api_key))
    with urllib.request.urlopen(request, timeout=180) as response:
        target.write_bytes(response.read())


def render_first_page(pdf_path: Path, png_path: Path, dpi: int) -> None:
    try:
        import fitz
    except ImportError as error:
        raise RuntimeError(
            "PyMuPDF is required to render PDF pages. Install it with: pip install pymupdf"
        ) from error

    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as document:
        if len(document) == 0:
            raise RuntimeError(f"PDF has no pages: {pdf_path}")

        page = document.load_page(0)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        pixmap.save(png_path)


def image_url(prefix: str, record_id: str) -> str:
    return f"{prefix.rstrip('/')}/{record_id}.png"


def update_training(args: argparse.Namespace) -> int:
    api_key = os.environ.get("ZENODO_API_KEY")
    training_path = args.training_json
    images_dir = args.images_dir

    entries = json.loads(training_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise RuntimeError(f"Expected {training_path} to contain a JSON list.")

    if not args.dry_run:
        images_dir.mkdir(parents=True, exist_ok=True)

    changed = False
    processed = 0

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue

        record_id = entry_record_id(entry)
        if not record_id:
            continue

        title = entry.get("title", f"entry {index}")
        target_png = images_dir / f"{record_id}.png"
        target_url = image_url(args.image_url_prefix, record_id)

        if entry.get("image") and not args.overwrite_images:
            print(f"skip existing image: {record_id} - {title}")
            continue

        print(f"process {record_id}: {title}")
        processed += 1

        if args.dry_run:
            print(f"  would write {target_png} and set image to {target_url}")
            continue

        try:
            record = zenodo_record(record_id, api_key)
            pdf_file = choose_pdf_file(record)
            if not pdf_file:
                print(f"  no PDF files found in Zenodo record {record_id}")
                continue

            pdf_url = download_url(pdf_file)
            if not pdf_url:
                print(f"  no download URL found for {file_name(pdf_file)}")
                continue

            if args.force or not target_png.exists():
                with tempfile.TemporaryDirectory() as tmpdir:
                    temp_pdf = Path(tmpdir) / safe_file_name(file_name(pdf_file))
                    print(f"  download {file_name(pdf_file)}")
                    download_file(pdf_url, temp_pdf, api_key)
                    print(f"  render {target_png}")
                    render_first_page(temp_pdf, target_png, args.dpi)
            else:
                print(f"  keep existing {target_png}")

            entry["image"] = target_url
            changed = True

        except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as error:
            print(f"  failed: {error}", file=sys.stderr)

    if changed and not args.dry_run:
        training_path.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"updated {training_path}")

    print(f"processed {processed} Zenodo-linked entries")
    return 0


def main() -> int:
    args = parse_args()
    return update_training(args)


if __name__ == "__main__":
    raise SystemExit(main())
