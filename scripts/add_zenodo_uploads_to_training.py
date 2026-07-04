#!/usr/bin/env python3
"""Add training entries for matching Zenodo uploads.

The script scans authenticated Zenodo upload/deposition records and adds records
to data/training.json when all of these are true:

* the record is not already referenced in training.json
* the record files contain at least one PDF
* the record files contain at least one PPTX

Authentication:
    Set ZENODO_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ZENODO_RECORD_RE = re.compile(r"zenodo\.org/(?:record|records)/(\d+)", re.IGNORECASE)
ZENODO_DOI_RE = re.compile(r"(?:doi\.org/)?10\.5281/zenodo\.(\d+)", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add Zenodo uploads with PDF and PPTX files to data/training.json."
    )
    parser.add_argument(
        "--training-json",
        default="data/training.json",
        type=Path,
        help="Path to the training JSON file.",
    )
    parser.add_argument(
        "--zenodo-api-url",
        default="https://zenodo.org/api",
        help="Base Zenodo API URL.",
    )
    parser.add_argument(
        "--label",
        default="Slides",
        help="Link label used for added Zenodo records.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print records that would be added without writing training.json.",
    )
    parser.add_argument(
        "--include-unpublished",
        action="store_true",
        help="Also add unpublished uploads that already have a concept/record ID.",
    )
    return parser.parse_args()


def request_headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def request_json(url: str, api_key: str) -> Any:
    request = urllib.request.Request(url, headers=request_headers(api_key))
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def zenodo_record_id(url: str) -> str | None:
    for regex in (ZENODO_RECORD_RE, ZENODO_DOI_RE):
        match = regex.search(url)
        if match:
            return match.group(1)
    return None


def existing_record_ids(entries: list[Any]) -> set[str]:
    record_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        for link in entry.get("links", []):
            if not isinstance(link, dict):
                continue

            url = link.get("url")
            if not isinstance(url, str):
                continue

            record_id = zenodo_record_id(url)
            if record_id:
                record_ids.add(record_id)

    return record_ids


def paged_depositions(api_url: str, api_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page = 1

    while True:
        query = urllib.parse.urlencode(
            {
                "page": page,
                "size": 100,
                "sort": "mostrecent",
            }
        )
        url = f"{api_url.rstrip('/')}/deposit/depositions?{query}"
        page_records = request_json(url, api_key)

        if not isinstance(page_records, list):
            raise RuntimeError(f"Unexpected Zenodo response for {url}")

        records.extend(
            record for record in page_records if isinstance(record, dict)
        )

        if len(page_records) < 100:
            break

        page += 1

    return records


def file_name(file_entry: dict[str, Any]) -> str:
    value = file_entry.get("filename") or file_entry.get("key") or file_entry.get("name")
    return str(value or "")


def file_extension(file_entry: dict[str, Any]) -> str:
    return Path(file_name(file_entry).lower()).suffix


def has_file_type(files: list[Any], extension: str) -> bool:
    expected_mimetypes = {
        ".pdf": {"application/pdf", "pdf"},
        ".pptx": {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pptx",
        },
    }

    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue

        mimetype = str(
            file_entry.get("mimetype") or file_entry.get("type") or ""
        ).lower()
        if (
            file_extension(file_entry) == extension
            or mimetype in expected_mimetypes.get(extension, set())
        ):
            return True
    return False


def deposition_files(deposition: dict[str, Any], api_key: str) -> list[Any]:
    files = deposition.get("files")
    if isinstance(files, list):
        return files

    links = deposition.get("links")
    files_url = links.get("files") if isinstance(links, dict) else None
    if not isinstance(files_url, str):
        return []

    files = request_json(files_url, api_key)
    return files if isinstance(files, list) else []


def record_id(deposition: dict[str, Any]) -> str | None:
    for key in ("record_id", "recid", "id"):
        value = deposition.get(key)
        if value:
            return str(value)
    return None


def publication_year(metadata: dict[str, Any], deposition: dict[str, Any]) -> int | None:
    for key in ("publication_date", "created", "modified"):
        value = metadata.get(key) or deposition.get(key)
        if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return None


def venue(metadata: dict[str, Any]) -> str | None:
    candidates = [
        metadata.get("conference_title"),
        metadata.get("conference_acronym"),
        metadata.get("meeting_title"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def creator_name_matches(name: str) -> bool:
    normalized = " ".join(name.replace(",", " ").split()).lower()
    return normalized in {"robert haase", "haase robert"}


def has_robert_haase_creator(metadata: dict[str, Any]) -> bool:
    creators = metadata.get("creators")
    if not isinstance(creators, list):
        return False

    for creator in creators:
        if not isinstance(creator, dict):
            continue

        name = creator.get("name")
        if isinstance(name, str) and creator_name_matches(name):
            return True

    return False


def public_record_url(deposition: dict[str, Any], record_id_value: str) -> str:
    for key in ("doi_url", "record_url"):
        value = deposition.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    doi = deposition.get("doi")
    if isinstance(doi, str) and doi.strip():
        return f"https://doi.org/{doi.strip()}"

    metadata = deposition.get("metadata")
    doi = metadata.get("doi") if isinstance(metadata, dict) else None
    if isinstance(doi, str) and doi.startswith("10.5281/zenodo."):
        return f"https://doi.org/{doi}"

    links = deposition.get("links")
    if isinstance(links, dict):
        record_html = links.get("record_html")
        latest_html = links.get("latest_html")
        for value in (record_html, latest_html):
            if isinstance(value, str) and "zenodo.org" in value:
                return value

    return f"https://zenodo.org/records/{record_id_value}"


def training_entry(deposition: dict[str, Any], record_id_value: str, label: str) -> dict[str, Any]:
    metadata = deposition.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    entry: dict[str, Any] = {}
    year = publication_year(metadata, deposition)
    if year is not None:
        entry["year"] = year

    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        title = deposition.get("title")
    entry["title"] = (
        title.strip()
        if isinstance(title, str) and title.strip()
        else f"Zenodo record {record_id_value}"
    )

    venue_value = venue(metadata)
    if venue_value:
        entry["venue"] = venue_value

    entry["links"] = [
        {
            "label": label,
            "url": public_record_url(deposition, record_id_value),
        }
    ]
    return entry


def should_include(deposition: dict[str, Any], include_unpublished: bool) -> bool:
    if include_unpublished:
        return True
    submitted = deposition.get("submitted")
    state = deposition.get("state")
    return submitted is True or state == "done"


def update_training(args: argparse.Namespace) -> int:
    api_key = os.environ.get("ZENODO_API_KEY")
    if not api_key:
        raise RuntimeError("ZENODO_API_KEY is not set.")

    entries = json.loads(args.training_json.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise RuntimeError(f"Expected {args.training_json} to contain a JSON list.")

    known_ids = existing_record_ids(entries)
    new_entries: list[dict[str, Any]] = []

    for deposition in paged_depositions(args.zenodo_api_url, api_key):
        current_record_id = record_id(deposition)
        if not current_record_id:
            continue

        if current_record_id in known_ids:
            continue

        if not should_include(deposition, args.include_unpublished):
            continue

        metadata = deposition.get("metadata")
        if not isinstance(metadata, dict) or not has_robert_haase_creator(metadata):
            continue

        files = deposition_files(deposition, api_key)

        if not (has_file_type(files, ".pdf") and has_file_type(files, ".pptx")):
            continue

        entry = training_entry(deposition, current_record_id, args.label)
        new_entries.append(entry)
        known_ids.add(current_record_id)
        print(f"add {current_record_id}: {entry['title']}")

    if not new_entries:
        print("No new Zenodo upload records found.")
        return 0

    entries[:0] = new_entries

    if args.dry_run:
        print(f"Would add {len(new_entries)} entries to {args.training_json}.")
        return 0

    args.training_json.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Added {len(new_entries)} entries to {args.training_json}.")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return update_training(args)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError) as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
