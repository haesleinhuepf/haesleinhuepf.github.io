#!/usr/bin/env python3
"""Resize local training preview images and update their JSON references.

The script scans data/training.json for entries whose image property starts
with "images/". For each matching entry, it opens the referenced image file,
resizes it to 500 px width while preserving the aspect ratio, saves it back to
the same file, and changes the JSON image value to a GitHub raw markdown link.

Requires:
    pip install pillow
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_GITHUB_RAW_PREFIX = (
    "https://github.com/haesleinhuepf/haesleinhuepf.github.io"
    "/blob/master/images"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resize local training images to 500 px width and update JSON links."
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
        help="Directory containing the referenced image files.",
    )
    parser.add_argument(
        "--width",
        default=500,
        type=int,
        help="Target image width in pixels.",
    )
    parser.add_argument(
        "--github-raw-prefix",
        default=DEFAULT_GITHUB_RAW_PREFIX,
        help="URL prefix used in the markdown image link.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without resizing images or writing JSON.",
    )
    return parser.parse_args()


def markdown_image_link(prefix: str, record_id: str) -> str:
    filename = f"{record_id}.png"
    return f"{prefix.rstrip('/')}/{filename}?raw=true"


def local_image_path(image_value: str, images_dir: Path) -> Path | None:
    image_path = Path(image_value)
    parts = image_path.parts
    if len(parts) < 2 or parts[0] != "images":
        return None

    return images_dir / Path(*parts[1:])


def resize_image(image_path: Path, width: int) -> bool:
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "Pillow is required to resize images. Install it with: pip install pillow"
        ) from error

    with Image.open(image_path) as image:
        current_width, current_height = image.size
        if current_width <= 0 or current_height <= 0:
            raise RuntimeError(f"Invalid image dimensions for {image_path}")

        target_height = max(1, round(current_height * width / current_width))
        if current_width == width:
            return False

        resized = image.resize((width, target_height), Image.Resampling.LANCZOS)
        resized.save(image_path)
        return True


def update_training(args: argparse.Namespace) -> int:
    if args.width <= 0:
        raise RuntimeError("--width must be greater than zero.")

    entries = json.loads(args.training_json.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise RuntimeError(f"Expected {args.training_json} to contain a JSON list.")

    changed = False
    processed = 0
    resized = 0
    missing = 0

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue

        image_value = entry.get("image")
        if not isinstance(image_value, str) or not image_value.startswith("images/"):
            continue

        image_path = local_image_path(image_value, args.images_dir)
        if image_path is None:
            continue

        record_id = image_path.stem
        target_value = markdown_image_link(args.github_raw_prefix, record_id)
        title = entry.get("title", f"entry {index}")
        processed += 1

        if not image_path.exists():
            missing += 1
            print(f"missing image: {image_path} - {title}", file=sys.stderr)
            continue

        print(f"process {record_id}: {title}")
        if args.dry_run:
            print(f"  would resize {image_path} to {args.width} px width")
            print(f"  would set image to {target_value}")
            continue

        if resize_image(image_path, args.width):
            resized += 1
            print(f"  resized {image_path}")
        else:
            print(f"  already {args.width} px wide")

        if entry["image"] != target_value:
            entry["image"] = target_value
            changed = True

    if changed and not args.dry_run:
        args.training_json.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"updated {args.training_json}")

    print(
        f"processed {processed} local image entries, resized {resized}, missing {missing}"
    )
    return 0


def main() -> int:
    args = parse_args()
    try:
        return update_training(args)
    except RuntimeError as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
