#!/usr/bin/env python3
"""Compare an archive to a dated, independently sourced Version of Record figure index."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

DOI_RE = re.compile(r"10\.\d{4,9}/\S+", re.I)
INDEX_CATEGORIES = ("graphical_abstract", "main", "extended_data", "supplementary")
REFERENCE_KINDS = {"publisher_figure_index", "publisher_page", "publisher_pdf"}
VERIFIED_LABEL_SOURCES = {"xml", "xml_group", "xml_category"}
SHA256_RE = re.compile(r"[0-9a-f]{64}", re.I)


def normalize_doi(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", value.strip(), flags=re.I)
    return text.casefold() if DOI_RE.fullmatch(text) else None


def label_key(category: str, label: str) -> str:
    label = " ".join(unicodedata.normalize("NFKC", label).casefold().split()).rstrip(" .:")
    if category == "graphical_abstract":
        return "graphical abstract" if label in {"graphical abstract", "graphical-abstract", "ga"} else label
    prefixes = {"extended_data": r"(?:extended|ext\.?)\s*data\s*",
                "supplementary": r"supp(?:lementary|lemental)?\.?\s*"}
    label = re.sub("^" + prefixes[category], "", label) if category in prefixes else label
    number = re.fullmatch(r"(?:fig(?:ure)?\.?\s*)?([a-z]?)\s*([0-9]{1,8})([a-z]?)", label)
    if number:
        prefix, digits, suffix = number.groups()
        return f"{prefix}{int(digits)}{suffix}"
    return label


def is_evidence_url(value: object) -> bool:
    if not isinstance(value, str) or re.search(r"[\x00-\x20<>]", value):
        return False
    try:
        url = urlsplit(value)
        return url.scheme == "https" and bool(url.hostname) and not (url.username or url.password)
    except ValueError:
        return False


def read_index(path: Path) -> dict[str, dict]:
    """Load one or more references; an explicit category can have zero figures."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    articles = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(articles, list) or not articles:
        raise ValueError("VoR index requires a non-empty 'articles' list")

    index: dict[str, dict] = {}
    for position, article in enumerate(articles, 1):
        if not isinstance(article, dict):
            raise ValueError(f"VoR index article {position} must be an object")
        doi = normalize_doi(article.get("doi"))
        if not doi or doi in index:
            raise ValueError(f"Invalid or duplicate DOI in VoR index article {position}")
        source = article.get("source")
        if not isinstance(source, dict) or source.get("kind") not in REFERENCE_KINDS:
            raise ValueError(f"{doi}: set source.kind to a supported publisher reference type")
        if not is_evidence_url(source.get("url")):
            raise ValueError(f"{doi}: source.url requires a public HTTPS evidence link")
        checked_on = source.get("checked_on")
        if not isinstance(checked_on, str) or date.fromisoformat(checked_on).isoformat() != checked_on:
            raise ValueError(f"{doi}: source.checked_on requires YYYY-MM-DD")
        categories = article.get("categories")
        if (not isinstance(categories, list) or not categories or
                any(cat not in INDEX_CATEGORIES for cat in categories) or
                len(set(categories)) != len(categories)):
            raise ValueError(f"{doi}: declare each verified figure category once")
        rows = article.get("figures")
        if not isinstance(rows, list):
            raise ValueError(f"{doi}: figures must be a list")
        seen = set()
        figures = []
        for row in rows:
            if not isinstance(row, dict) or row.get("category") not in categories:
                raise ValueError(f"{doi}: every figure category must be in the verified scope")
            category, label = row["category"], row.get("label")
            key = label_key(category, label) if isinstance(label, str) else ""
            if not key or (category, key) in seen:
                raise ValueError(f"{doi}: empty or duplicate figure label in {category}")
            seen.add((category, key))
            figure = {"category": category, "label": label.strip()}
            if isinstance(row.get("locator"), str) and row["locator"].strip():
                figure["locator"] = row["locator"].strip()
            if "evidence_url" in row:
                if not is_evidence_url(row["evidence_url"]):
                    raise ValueError(f"{doi}: figure evidence_url requires a public HTTPS link")
                figure["evidence_url"] = row["evidence_url"]
            figures.append(figure)
        index[doi] = {"source": {key: source[key] for key in ("kind", "url", "checked_on")},
                      "categories": categories, "figures": figures}
    return index


def check_asset(folder: Path, record: dict) -> dict:
    name = record.get("file")
    result = {"file": name, "sha256": record.get("sha256"), "width": record.get("width"),
              "height": record.get("height"), "quality_tier": record.get("quality_tier") or
              "source_resolution_unverified", "status": "unavailable"}
    if record.get("status") != "downloaded" or not name:
        result["error"] = record.get("error")
        return result
    try:
        if not isinstance(name, str):
            result["status"] = "invalid_path"
            return result
        path = (folder / name).resolve()
        if not path.is_relative_to(folder):
            result["status"] = "invalid_path"
        elif not path.is_file():
            result["status"] = "missing_file"
        elif not SHA256_RE.fullmatch(record.get("sha256") or ""):
            result["status"] = "missing_hash"
        else:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result["status"] = ("verified" if digest.hexdigest() == record["sha256"].lower()
                                else "hash_mismatch")
    except (OSError, RuntimeError, ValueError):
        result["status"] = "unreadable_file"
    return result


def record_summary(figure: dict) -> dict:
    return {"id": figure.get("id"), "group_id": figure.get("group_id"),
            "label": figure.get("label"), "category": figure.get("category"),
            "label_source": figure.get("label_source") or "legacy_manifest"}


def compare(
    manifest: dict, index: dict[str, dict] | None, folder: Path, doi_override: str | None = None
) -> dict:
    metadata = manifest["metadata"]
    recorded_doi = normalize_doi(metadata.get("doi")) or normalize_doi(metadata.get("input"))
    explicit_doi = normalize_doi(doi_override)
    if doi_override is not None and (not explicit_doi or recorded_doi and explicit_doi != recorded_doi):
        raise ValueError("--doi must be valid and agree with any DOI recorded in the manifest")
    doi = recorded_doi or explicit_doi
    version = metadata.get("source_version") or "unknown"
    report = {"status": "not_checked", "doi": doi, "source_version": version,
              "doi_source": "manifest" if recorded_doi else "user" if explicit_doi else None,
              "version_status": "author_manuscript" if version == "author_manuscript" else "not_verified"}
    if index is None:
        return {**report, "reason": "no_reference_supplied"}
    if doi is None:
        return {**report, "reason": "article_doi_unknown"}
    if doi not in index:
        return {**report, "reason": "no_reference_for_doi"}

    reference = index[doi]
    scope = reference["categories"]
    source_figures: dict[tuple[str, str], list[dict]] = defaultdict(list)
    unresolved, unexpected, results = [], [], []
    for figure in manifest["figures"]:
        category = figure["category"]
        summary = record_summary(figure)
        if category == "unclassified":
            unresolved.append(summary)
        if category not in scope:
            continue
        key = label_key(category, figure.get("label") or "")
        if not key or summary["label_source"] in {"inferred", "object_reference"}:
            unresolved.append(summary)
            continue
        source_figures[(category, key)].append(figure)

    counts = dict(expected=len(reference["figures"]), matched_labels=0, verified_figures=0,
                  captioned_figures=0, lower_resolution_figures=0, missing_records=0,
                  ambiguous=0, unverified_labels=0, unavailable_assets=0)
    folder = folder.resolve()
    for expected in reference["figures"]:
        key = (expected["category"], label_key(expected["category"], expected["label"]))
        found = source_figures.pop(key, [])
        row = {**expected, "source_records": [record_summary(f) for f in found]}
        if not found:
            row["status"] = "missing_record"
            counts["missing_records"] += 1
        elif len(found) > 1:
            row["status"] = "ambiguous"
            counts["ambiguous"] += 1
        else:
            figure = found[0]
            confirmed_label = figure.get("label_source") in VERIFIED_LABEL_SOURCES
            row["caption_available"] = bool(figure.get("caption"))
            row["assets"] = [check_asset(folder, asset) for asset in figure.get("images", [])]
            row["lower_resolution"] = any(
                asset["status"] == "verified" and
                asset["quality_tier"].startswith(("IMAGE-DOWNSAMPLED", "IMAGE-THUMBNAIL", "pdf_crop"))
                for asset in row["assets"]
            )
            files_ok = (figure.get("status") == "downloaded" and bool(row["assets"]) and
                        all(asset["status"] == "verified" for asset in row["assets"]))
            counts["matched_labels"] += confirmed_label
            counts["unverified_labels"] += not confirmed_label
            counts["captioned_figures"] += confirmed_label and row["caption_available"]
            counts["verified_figures"] += confirmed_label and files_ok
            counts["lower_resolution_figures"] += row["lower_resolution"]
            counts["unavailable_assets"] += not files_ok
            if not files_ok:
                row["status"] = "asset_unavailable"
            else:
                row["status"] = "matched" if confirmed_label else "unverified_label"
        results.append(row)

    for figures in source_figures.values():
        unexpected.extend(record_summary(f) for f in figures)
    counts.update(unexpected_records=len(unexpected), unresolved_records=len(unresolved))
    if counts["missing_records"] or counts["unavailable_assets"]:
        status = "incomplete"
    elif any((unexpected, unresolved, counts["ambiguous"], counts["unverified_labels"],
              version == "author_manuscript", bool(manifest.get("errors")))):
        status = "needs_review"
    else:
        status = "matched"
    return {**report, "status": status, "reference": reference["source"], "categories": scope,
            "counts": counts, "figures": results, "unexpected_records": unexpected,
            "unresolved_records": unresolved}


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path, help="Existing collection manifest.json")
    parser.add_argument("--vor-index", required=True, type=Path)
    parser.add_argument("--doi", help="Explicit DOI for legacy manifests that lack article identity")
    args = parser.parse_args()
    try:
        index = read_index(args.vor_index)
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        report = compare(manifest, index, args.manifest.parent, args.doi)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["status"] == "matched" else 2


if __name__ == "__main__":
    sys.exit(cli())
