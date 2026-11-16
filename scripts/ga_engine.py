"""Graphical abstract evidence, acquisition and durable per-rendition reporting."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from lxml import etree
from PIL import Image

STATUS = {
    "downloaded",
    "detected_not_accessible",
    "candidate_needs_review",
    "not_found_in_source",
    "confirmed_not_provided",
    "unknown",
}
MAX_BYTES = 80 * 1024 * 1024
IMAGE_EXT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "TIFF": ".tif",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "SVG": ".svg",
    "EPS": ".eps",
    "PDF": ".pdf",
}


@dataclass(frozen=True)
class Source:
    doi: str | None
    kind: str
    url: str | None = None
    license: str | None = None
    version: str = "unknown"
    version_id: str | None = None


@dataclass(frozen=True)
class Variant:
    reference: str
    quality: str = "source_resolution_unverified"
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class Slot:
    id: str
    owner: str
    edition: str
    variants: tuple[Variant, ...]
    caption: str = ""
    attribution: str = ""
    evidence: str = ""
    locator: str = ""
    license: str | None = None
    blocked_code: str | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class Candidate:
    reference: str
    reason_code: str
    reason: str


@dataclass
class Discovery:
    slots: list[Slot] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    scanned: bool = False
    reason: str = ""
    scopes: tuple = ()


class AcquisitionError(OSError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def clean_reference(value: str | None) -> str | None:
    """Keep the location and public object selectors while dropping credentials."""
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if not parsed.scheme and not parsed.netloc:
            return parsed.path
        if parsed.scheme == "file":
            return (
                parsed.path
                and parsed._replace(netloc="", query="", fragment="").geturl()
            )
        if parsed.scheme not in {"https", "http", "s3"} or not parsed.hostname:
            return "[unverified resource]"
        keep = []
        for key, item in parse_qsl(parsed.query):
            if (
                (key == "view" and item.casefold() in {"high", "standard", "thumbnail"})
                or (
                    key == "httpAccept"
                    and re.fullmatch(r"(?:image/[a-z+.-]+|\*/\*)", item)
                )
                or (key == "md5" and re.fullmatch(r"[0-9a-f]{32}", item, re.I))
            ):
                keep.append((key, item))
        return parsed._replace(
            netloc=parsed.hostname, query=urlencode(keep), fragment=""
        ).geturl()
    except ValueError:
        return "[unverified resource]"


def failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AcquisitionError):
        return exc.code, str(exc)
    if isinstance(exc, requests.RequestException):
        if exc.response is not None:
            status = exc.response.status_code
            return f"http_{status}", f"HTTP {status} from the source"
        return "network_error", f"{type(exc).__name__} while requesting the source"
    if isinstance(exc, FileNotFoundError):
        return "media_not_listed", "The exact reference has no authorized media entry"
    if isinstance(exc, PermissionError):
        return (
            "access_denied",
            "The requested source media is outside the authorized access scope",
        )
    if isinstance(exc, ValueError):
        return "invalid_media", "The source did not return a valid figure image"
    return "source_error", f"{type(exc).__name__} while processing source media"


def dimensions(data: bytes) -> tuple[str, float | int | None, float | int | None, str]:
    if not 0 < len(data) <= MAX_BYTES:
        raise AcquisitionError(
            "invalid_size", "The source figure is empty or exceeds 80 MiB"
        )
    if data.startswith(b"%PDF-"):
        try:
            import pymupdf as fitz

            with fitz.open(stream=data, filetype="pdf") as pdf:
                if len(pdf) != 1:
                    raise AcquisitionError(
                        "invalid_media",
                        "A standalone figure PDF must contain a single page",
                    )
                size = pdf[0].rect
                return "PDF", size.width, size.height, "pt"
        except ImportError:
            return "PDF", None, None, "unknown"
    if data.startswith((b"%!PS", b"\xc5\xd0\xd3\xc6")):
        if b"EPSF" not in data[:1024]:
            raise AcquisitionError(
                "invalid_media", "The object is not a standalone EPS figure"
            )
        bounds = re.search(
            rb"^%%(?:HiRes)?BoundingBox:\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)",
            data,
            re.M,
        )
        if bounds:
            x, y, x1, y1 = map(float, bounds.groups())
            return "EPS", max(0, x1 - x), max(0, y1 - y), "pt"
        return "EPS", None, None, "unknown"
    if data.lstrip().startswith(b"<"):
        doc = etree.fromstring(
            data,
            etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False),
        )
        if doc.tag != "{http://www.w3.org/2000/svg}svg" and doc.tag != "svg":
            raise AcquisitionError(
                "invalid_media", "The source returned markup outside SVG"
            )
        x, y = (
            re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:px)?", doc.get(k, ""))
            for k in ("width", "height")
        )
        if x and y and float(x[1]) and float(y[1]):
            return "SVG", float(x[1]), float(y[1]), "svg_units"
        view = doc.get("viewBox", "").replace(",", " ").split()
        if len(view) == 4 and all(re.fullmatch(r"-?[\d.]+", v) for v in view):
            return "SVG", float(view[2]), float(view[3]), "svg_viewbox_units"
        return "SVG", None, None, "unknown"
    with Image.open(BytesIO(data)) as image:
        fmt = image.format
        width, height = image.size
        image.verify()
    if fmt not in IMAGE_EXT:
        raise AcquisitionError(
            "invalid_format",
            "The image format is outside the supported archive formats",
        )
    return fmt, width, height, "px"


def save_asset(
    folder: Path,
    data: bytes,
    url: str,
    slot: Slot,
    source: Source,
    variant: Variant,
    *,
    fallback: bool = False,
    method: str = "original",
    crop: dict | None = None,
) -> dict:
    fmt, width, height, unit = dimensions(data)
    if (variant.width is not None and (unit != "px" or width != variant.width)) or (
        variant.height is not None and (unit != "px" or height != variant.height)
    ):
        raise AcquisitionError(
            "dimensions_mismatch",
            "Image dimensions differ from the publisher object rendition",
        )
    digest = sha256(data).hexdigest()
    directory = folder / "graphical_abstract"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve().parent != folder.resolve():
        raise AcquisitionError(
            "unsafe_output",
            "The graphical abstract directory points outside the archive",
        )
    filename = f"{slot.edition}-{method}-{digest}{IMAGE_EXT[fmt]}"
    target = directory / filename
    if target.is_symlink():
        raise AcquisitionError(
            "unsafe_output", "The target media path is a symbolic link"
        )
    if not target.is_file() or sha256(target.read_bytes()).hexdigest() != digest:
        temp = None
        try:
            with NamedTemporaryFile(
                mode="wb", dir=directory, suffix=".tmp", delete=False
            ) as stream:
                temp = Path(stream.name)
                stream.write(data)
            temp.replace(target)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    return {
        "file": target.relative_to(folder).as_posix(),
        "format": fmt,
        "width": width,
        "height": height,
        "dimension_unit": unit,
        "sha256": digest,
        "source_url": clean_reference(url),
        "source_version": source.version,
        "source_version_id": source.version_id,
        "doi": source.doi,
        "license": slot.license or source.license,
        "edition": slot.edition,
        "acquisition_method": method,
        "quality_tier": "pdf_crop"
        if method == "pdf_crop"
        else variant.quality
        + ("_fallback" if fallback and variant.quality.startswith("IMAGE-") else ""),
        "fallback_variant": fallback,
        **({"pdf_crop": crop} if crop else {}),
    }


def acquire(
    discovery: Discovery,
    source: Source,
    folder: Path,
    fetch: Callable[[Variant], tuple[bytes, str]],
    *,
    pdf: Callable | None = None,
    absence_url: str | None = None,
) -> dict:
    records = []
    observations = [
        {
            "source": source.kind,
            "source_url": clean_reference(source.url),
            "source_version": source.version,
            "scanned": discovery.scanned,
            "reason": discovery.reason
            or (
                "Graphical abstract roles inspected"
                if discovery.scanned
                else "Source was not available for inspection"
            ),
        }
    ]
    for slot in discovery.slots:
        row = {
            "id": slot.id,
            "owner": slot.owner,
            "edition": slot.edition,
            "caption": slot.caption,
            "attribution": slot.attribution,
            "evidence": slot.evidence,
            "locator": slot.locator,
            "source": source.kind,
            "evidence_url": clean_reference(source.url),
            "reference": clean_reference(slot.variants[0].reference)
            if slot.variants
            else None,
            "status": "detected_not_accessible",
            "asset": None,
            "attempts": [],
            "reason_code": slot.blocked_code or "missing_reference",
            "reason": slot.blocked_reason
            or "Explicit graphical abstract has no usable image reference",
        }
        for i, variant in enumerate(slot.variants):
            try:
                binary, url = fetch(variant)
                asset = save_asset(
                    folder, binary, url, slot, source, variant, fallback=i > 0
                )
                row.update(
                    status="downloaded", asset=asset, reason_code=None, reason=None
                )
                break
            except (
                OSError,
                ValueError,
                requests.RequestException,
                etree.XMLSyntaxError,
                Image.DecompressionBombError,
                RuntimeError,
            ) as exc:
                code, detail = failure(exc)
                row.update(reason_code=code, reason=detail)
                row["attempts"].append(
                    {
                        "reference": clean_reference(variant.reference),
                        "reason_code": code,
                        "reason": detail,
                    }
                )
        records.append(row)
    if (
        pdf is not None
        and not any(row["status"] == "downloaded" for row in records)
        and not absence_url
    ):
        try:
            binary, url, crop = pdf()
            slot = Slot(
                "pdf_crop",
                "graphical_abstract",
                "print_only",
                (),
                evidence="Explicit PDF Graphical Abstract heading",
            )
            pdf_source = replace(
                source, kind="publisher_pdf", url=url, version="publisher_pdf"
            )
            asset = save_asset(
                folder,
                binary,
                url,
                slot,
                pdf_source,
                Variant(url),
                method="pdf_crop",
                crop=crop,
            )
            records.append(
                {
                    "id": slot.id,
                    "owner": slot.owner,
                    "edition": slot.edition,
                    "caption": "",
                    "attribution": "",
                    "evidence": slot.evidence,
                    "locator": f"PDF page {crop['page']}",
                    "source": "publisher_pdf",
                    "evidence_url": clean_reference(url),
                    "reference": clean_reference(url),
                    "status": "downloaded",
                    "asset": asset,
                    "reason": None,
                    "reason_code": None,
                    "attempts": [],
                }
            )
        except (OSError, ValueError, RuntimeError) as exc:
            observations.append(
                {
                    "source": "publisher_pdf",
                    "scanned": False,
                    "reason_code": "pdf_crop_unavailable",
                    "reason": str(exc)
                    if isinstance(exc, AcquisitionError)
                    else "No unambiguous graphical abstract crop was verified",
                }
            )
    candidates = [
        {**asdict(item), "reference": clean_reference(item.reference)}
        for item in discovery.candidates
    ]
    return make_report(
        source.doi, records, candidates, observations, absence_url=absence_url
    )


def make_report(doi, records, candidates, observations, *, absence_url=None) -> dict:
    downloaded = sum(row["status"] == "downloaded" for row in records)
    if downloaded:
        status = "downloaded"
    elif records:
        status = "detected_not_accessible"
    elif absence_url:
        status = "confirmed_not_provided"
    elif candidates:
        status = "candidate_needs_review"
    else:
        status = (
            "not_found_in_source"
            if any(row["scanned"] for row in observations)
            else "unknown"
        )
    reason = next((row.get("reason") for row in records if row.get("reason")), None)
    if reason is None:
        reason = (
            next((c["reason"] for c in candidates), None)
            if status == "candidate_needs_review"
            else None
        )
    if reason is None and status != "downloaded":
        reason = {
            "confirmed_not_provided": "Independent publication inventory confirms absence",
            "not_found_in_source": "No explicit graphical abstract role in the inspected sources",
            "unknown": "No verifiable article source was processed",
        }.get(status)
    return {
        "doi": doi,
        "status": status,
        "reason": reason,
        "absence_evidence_url": clean_reference(absence_url),
        "records": records,
        "candidates": candidates,
        "sources": observations,
        "counts": {
            "detected": len(records),
            "downloaded": downloaded,
            "unique_files": len({r["asset"]["file"] for r in records if r["asset"]}),
            "unavailable": len(records) - downloaded,
            "reasons": dict(
                Counter(r["reason_code"] for r in records if r["reason_code"])
            ),
        },
    }
