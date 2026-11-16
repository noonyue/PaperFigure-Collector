#!/usr/bin/env python3
"""Authorized publisher figure extraction for PaperFigure-Collector v0.3.

PMC remains the preferred pathway. Publisher endpoints are used only with explicitly
provided, entitled API credentials. This module never scrapes article web pages,
solves CAPTCHAs, guesses image CDN paths, or downloads subscription content without
appropriate API credentials/authorization. No downloaded figure is presumed reusable.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import requests
from lxml import etree
from PIL import Image

import collect_figures as core
import vor_audit

SPRINGER_API = "https://api.springernature.com/openaccess/jats"
ELSEVIER_API = "https://api.elsevier.com/content"
DOI_RE = vor_audit.DOI_RE
RIGHTS_NOTE = "Usage rights follow the publisher terms, article license and any figure-level credits."
ELSEVIER_RANK = {"IMAGE-HIGH-RES": 3, "IMAGE-DOWNSAMPLED": 2, "IMAGE-THUMBNAIL": 1}
IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "TIFF": ".tif", "GIF": ".gif",
                 "WEBP": ".webp", "BMP": ".bmp"}


class PublisherHTTPError(PermissionError):
    def __init__(self, status_code: int):
        super().__init__(f"Publisher API returned HTTP {status_code}")


def safe_root(xml: bytes) -> etree._Element:
    return core.jats_root(xml)


def children_named(node: etree._Element, name: str) -> list[etree._Element]:
    return [x for x in node if core.localname(x) == name]


def direct_text(node: etree._Element, name: str) -> str:
    return core.normalize_space(" ".join(" ".join(c.itertext()) for c in children_named(node, name)))


def provider_for(doi: str) -> str:
    doi = doi.lower()
    if doi.startswith(("10.1038/", "10.1007/", "10.1186/")):
        return "springer"
    if doi.startswith("10.1016/"):
        return "elsevier"
    if doi.startswith("10.1126/"):
        return "science"
    return "unknown"


def allowed_publisher_url(url: str, provider: str) -> bool:
    if re.search(r"[\x00-\x1f\x7f]", url):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or
                not parsed.hostname or parsed.port not in (None, 443)):
            return False
        path = unquote(parsed.path)
        if (re.search(r"[\x00-\x20\\]", path) or ".." in path.split("/") or "." in path.split("/")):
            return False
        if provider == "elsevier":
            return parsed.hostname == "api.elsevier.com" and path.startswith("/content/object/")
        if provider == "springer":
            return parsed.hostname in {"media.springernature.com", "static-content.springer.com"}
    except ValueError:
        pass
    return False


def public_reference(reference: str) -> str:
    """Keep traceable media locations while discarding query credentials and userinfo."""
    try:
        parsed = urlsplit(reference)
        if not parsed.scheme and not parsed.netloc:
            return parsed.path
        if not parsed.hostname:
            return "[invalid media URL]"
        hostname = parsed.hostname
        query = []
        if hostname == "api.elsevier.com":
            for key, value in parse_qsl(parsed.query):
                if ((key == "view" and value.casefold() in {"high", "thumbnail", "standard"}) or
                        (key == "httpAccept" and value in {"*/*", "image/jpeg", "image/png", "image/gif",
                                                            "image/tiff", "image/svg", "image/svg+xml",
                                                            "image/webp", "image/bmp"})):
                    query.append((key, value))
        return parsed._replace(netloc=hostname, query=urlencode(query), fragment="").geturl()
    except ValueError:
        return "[invalid media URL]"


def safe_http_get(session: requests.Session, url: str, *, params: dict | None = None,
                  headers: dict | None = None, max_size: int = 150 * 1024 * 1024) -> bytes:
    # URLs and credentials must never be displayed in errors or persisted as request headers.
    response = session.get(url, params=params, headers=headers, timeout=(10, 45),
                           allow_redirects=False, stream=True)
    try:
        if response.status_code != 200:
            raise PublisherHTTPError(response.status_code)
        parts = []
        size = 0
        for chunk in response.iter_content(chunk_size=1024 * 512):
            size += len(chunk)
            if size > max_size:
                raise ValueError("Response exceeds configured safety cap")
            parts.append(chunk)
        return b"".join(parts)
    finally:
        response.close()


def find_jats_article(response_xml: bytes, doi: str) -> bytes:
    """Select the Springer JATS record by its primary DOI."""
    root = safe_root(response_xml)
    expected = vor_audit.normalize_doi(doi)
    for article in root.iter():
        if core.localname(article) == "article" and expected and core.jats_article_metadata(article).get("doi") == expected:
            return etree.tostring(article, encoding="utf-8")
    raise LookupError("No matching DOI in Springer Nature OA JATS API response")


@dataclass(frozen=True)
class ElsevierRendition:
    ref: str
    url: str
    tier: str
    width: int | None
    height: int | None


def elsevier_object_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        # Elsevier's documented META sample uses legacy HTTP object URLs.
        if parsed.scheme == "http" and parsed.hostname == "api.elsevier.com":
            if parsed.username or parsed.password or parsed.port not in (None, 80):
                return None
            url = parsed._replace(scheme="https", netloc="api.elsevier.com").geturl()
        if allowed_publisher_url(url, "elsevier"):
            return public_reference(url)
    except ValueError:
        pass
    return None


def elsevier_renditions(data: bytes, doi: str | None = None) -> list[ElsevierRendition]:
    root = safe_root(data)
    if core.localname(root) != "attachment-metadata-response":
        raise LookupError("Elsevier META response has no attachment metadata")
    coredata = root.find("./{*}coredata")
    doi_node = coredata if coredata is not None else root
    declared = vor_audit.normalize_doi(direct_text(doi_node, "doi"))
    if doi and declared and declared != vor_audit.normalize_doi(doi):
        raise LookupError("Elsevier META DOI disagrees with the requested DOI")
    assets = []
    for item in children_named(root, "attachment"):
        ref = direct_text(item, "ref")
        mime = direct_text(item, "mimetype").lower()
        tier = direct_text(item, "type").upper()
        if not ref or not mime.startswith("image/") or tier not in ELSEVIER_RANK:
            continue
        url = elsevier_object_url(direct_text(item, "url"))
        if url is None:
            continue
        dims = [direct_text(item, name) for name in ("width", "height")]
        width, height = (int(value) if re.fullmatch(r"[1-9][0-9]{0,7}", value) else None for value in dims)
        assets.append(ElsevierRendition(ref, url, tier, width, height))
    return assets


def group_elsevier_renditions(renditions: list[ElsevierRendition]) -> list[core.Figure]:
    groups: dict[str, list[ElsevierRendition]] = {}
    for asset in renditions:
        groups.setdefault(asset.ref, []).append(asset)
    figures = []
    for ref, assets in groups.items():
        number = re.fullmatch(r"gr([0-9]{1,8})", ref, re.I)
        if number:
            category, label = "main", f"Figure {int(number.group(1))}"
        elif re.fullmatch(r"ga\d*|graphical[-_ ]?abstract", ref, re.I):
            category, label = "graphical_abstract", "Graphical Abstract"
        elif re.fullmatch(r"(?:sup|sfig|sf|mmc)\d+", ref, re.I):
            category, label = "supplementary", f"Supplementary asset {ref}"
        else:
            category, label = "unclassified", f"Unclassified asset {ref}"
        assets.sort(key=lambda asset: ELSEVIER_RANK[asset.tier], reverse=True)
        urls = list(dict.fromkeys(asset.url for asset in assets))
        figures.append(core.Figure(
            id=ref, label=label, caption="", attribution="", category=category,
            references=[urls[0]], variants={urls[0]: urls}, label_source="object_reference",
        ))
    def object_order(fig: core.Figure) -> tuple[bool, int, str]:
        number = re.search(r"[0-9]{1,8}", fig.id)
        return fig.category != "graphical_abstract", int(number.group()) if number else 0, fig.id

    return sorted(figures, key=object_order)


def parse_elsevier_attachments(data: bytes) -> list[core.Figure]:
    """Read the figure candidates from the documented Object Retrieval META payload."""
    return group_elsevier_renditions(elsevier_renditions(data))


def elsevier_quality_map(data: bytes) -> dict[str, str]:
    """Read Elsevier's documented rendition tier for each supplied media URL."""
    return {asset.url: asset.tier for asset in elsevier_renditions(data)}


def elsevier_fulltext_figures(data: bytes, doi: str | None) -> list[core.Figure]:
    root = safe_root(data)
    root_tag = core.localname(root)
    if root_tag == "full-text-retrieval-response":
        coredata = root.find("./{*}coredata")
        declared = vor_audit.normalize_doi(direct_text(coredata, "doi")) if coredata is not None else None
        if doi and declared and declared != vor_audit.normalize_doi(doi):
            raise LookupError("Elsevier FULL DOI disagrees with the requested DOI")
        original = root.find("./{*}originalText")
        if original is None:
            raise LookupError("Elsevier FULL response lacks structured originalText")
        root = next((node for node in original if core.localname(node) in {"doc", "article"}), None)
        if root is None:
            raise LookupError("Elsevier FULL response lacks structured originalText")
    elif root_tag not in {"doc", "article"}:
        raise LookupError("Elsevier FULL response lacks structured article XML")

    if core.localname(root) == "article":
        xml = etree.tostring(root, encoding="utf-8")
        article_doi = core.jats_article_metadata(root).get("doi")
        if doi and article_doi and article_doi != vor_audit.normalize_doi(doi):
            raise LookupError("Elsevier FULL JATS DOI disagrees with the requested DOI")
        return core.parse_jats(xml, include_supplementary=True)

    xocs_meta = root.find("./{*}meta")
    declared = vor_audit.normalize_doi(direct_text(xocs_meta, "doi")) if xocs_meta is not None else None
    if doi and declared and declared != vor_audit.normalize_doi(doi):
        raise LookupError("Elsevier XOCS DOI disagrees with the requested DOI")
    figures = []
    for element in root.iter():
        tag = core.localname(element)
        graphical = core.is_graphical_abstract(element)
        if tag not in {"fig", "figure"} and not graphical:
            continue
        if any(core.localname(parent) in {"figure", "fig", "sub-article", "bibliography", "table"}
               for parent in element.iterancestors()):
            continue
        if graphical and any(core.localname(child) in {"figure", "fig"} for child in element.iterdescendants()):
            continue

        locators = []
        for node in element.iterdescendants():
            if core.localname(node) not in {"link", "graphic", "inline-graphic"}:
                continue
            for parent in node.iterancestors():
                if parent is element:
                    locator = (node.get("locator") or node.get(core.XLINK) or node.get("href") or "").removeprefix("#")
                    if locator:
                        locators.append(locator)
                    break
                if core.localname(parent) in {"caption", "legend", "attrib", "footnote", "figure", "fig"}:
                    break

        label = direct_text(element, "label")
        category = "graphical_abstract" if graphical else core.figure_category(element)
        label_source = "xml" if label else "inferred"
        if not label and category == "graphical_abstract":
            label, label_source = "Graphical Abstract", "xml_category"
        number = len(figures) + 1
        figures.append(core.Figure(
            id=element.get("id") or f"fulltext_{number}", label=label or f"Unlabelled figure {number}",
            caption=direct_text(element, "caption"), attribution=direct_text(element, "attrib"),
            category=category, references=list(dict.fromkeys(locators)), label_source=label_source,
        ))
    return figures


def attach_elsevier_captions(figures: list[core.Figure], fulltext_xml: bytes, doi: str | None = None) -> int:
    """Reconcile FULL figures with META assets using exact, uniquely owned locators."""
    fulltext = elsevier_fulltext_figures(fulltext_xml, doi)
    if not fulltext:
        return 0
    meta = {fig.id: fig for fig in figures}
    usage = Counter(ref for fig in fulltext for ref in fig.references)
    consumed = set()
    matched_captions = 0
    for fig in fulltext:
        locators = fig.references
        fig.references = []
        fig.variants = {}
        for locator in locators:
            candidate = meta.get(locator)
            if candidate is None or usage[locator] != 1:
                # Keep the missing panel in the record even if the other panels are available.
                fig.references.append(locator)
                fig.variants[locator] = []
                continue
            fig.references.extend(candidate.references)
            fig.variants.update(candidate.variants)
            consumed.add(locator)
            if len(locators) == 1:
                fig.id = candidate.id
        if fig.caption and any(locator in consumed for locator in locators):
            matched_captions += 1
    figures[:] = fulltext + [fig for fig in figures if fig.id not in consumed]
    return matched_captions


def publisher_image_extension(data: bytes, url: str) -> str:
    ext = core.file_extension(url)
    if ext in {".svg", ".eps", ".pdf"}:
        return ext
    if data.lstrip().startswith((b"<svg", b"<?xml")):
        if core.localname(safe_root(data)) == "svg":
            return ".svg"
        raise ValueError("Response is not a figure image")
    with Image.open(BytesIO(data)) as image:
        kind = IMAGE_FORMATS.get(image.format)
    if not kind:
        raise ValueError("Unknown figure image format")
    return kind


def save_publisher_figures(figures: list[core.Figure], dest: Path,
                           s: requests.Session, provider: str, headers: dict[str, str] | None,
                           *, include_supplementary: bool = False,
                           quality_by_url: dict[str, str] | None = None,
                           renditions: dict[str, ElsevierRendition] | None = None) -> None:
    for idx, fig in enumerate(figures, 1):
        if fig.category == "supplementary" and not include_supplementary:
            continue
        folder = dest / fig.category
        folder.mkdir(parents=True, exist_ok=True)
        for j, ref in enumerate(fig.references, 1):
            record: dict[str, Any] = {
                "reference": public_reference(ref), "status": "missing", "file": None, "source_url": None,
                "sha256": None, "width": None, "height": None, "quality_tier": "unverified", "error": None,
                "attempts": [],
            }
            candidates = fig.variants.get(ref, [ref])
            for candidate in candidates:
                info = renditions.get(candidate) if renditions else None
                quality = (info.tier if info else
                           (quality_by_url or {}).get(candidate, "publisher_asset_resolution_unverified"))
                attempt = {"source_url": public_reference(candidate), "quality_tier": quality}
                try:
                    if not allowed_publisher_url(candidate, provider):
                        raise PermissionError("Source XML contains no allowlisted absolute media address")
                    data = safe_http_get(s, candidate, headers=headers)
                    kind = publisher_image_extension(data, candidate)
                    if provider == "elsevier" and kind in {".pdf", ".eps"}:
                        raise ValueError("Elsevier image rendition returned a non-image document")
                    width, height = core.verify_image_bytes(data, kind)
                    if info and ((info.width is not None and width != info.width) or
                                 (info.height is not None and height != info.height)):
                        raise ValueError("Image dimensions differ from the Elsevier META rendition")
                    path = folder / f"{idx:03d}_{core.clean_name(fig.id)}_{j:02d}{kind}"
                    path.write_bytes(data)
                    fallback = candidate != ref
                    record.update({
                        "status": "downloaded", "file": path.relative_to(dest).as_posix(),
                        "source_url": public_reference(candidate), "sha256": hashlib.sha256(data).hexdigest(),
                        "width": width, "height": height, "fallback_variant": fallback,
                        "quality_tier": quality + ("_fallback" if provider == "elsevier" and fallback else ""),
                    })
                    break
                except (ValueError, OSError, etree.XMLSyntaxError, Image.DecompressionBombError) as exc:
                    attempt["error"] = publisher_error(exc)
                    record["attempts"].append(attempt)
            if record["status"] != "downloaded":
                record["error"] = (". ".join(attempt["error"] for attempt in record["attempts"]) or
                                   "No eligible META attachment for the FULL figure locator")
            fig.images.append(record)
            time.sleep(0.08)
        fig.status = ("downloaded" if fig.images and all(x["status"] == "downloaded" for x in fig.images)
                      else "partial" if any(x["status"] == "downloaded" for x in fig.images)
                      else "missing")
        fig.references = [public_reference(ref) for ref in fig.references]
        fig.variants = {public_reference(ref): [public_reference(url) for url in urls]
                        for ref, urls in fig.variants.items()}


def publisher_error(exc: Exception) -> str:
    """Record controlled failure details rather than raw HTTP exceptions or URLs."""
    if isinstance(exc, requests.RequestException):
        if exc.response is not None:
            return f"HTTP {exc.response.status_code} from publisher API"
        return f"{type(exc).__name__} while requesting official publisher API"
    if isinstance(exc, (PermissionError, LookupError, ValueError)) and not isinstance(exc, UnicodeError):
        return str(exc)[:220]
    return f"{type(exc).__name__} while processing publisher response"


def collect_publisher(s: requests.Session, doi: str, folder: Path, provider: str,
                      include_supplementary: bool, *, vor_index: dict[str, dict] | None = None) -> dict[str, Any]:
    figures: list[core.Figure] = []
    metadata: dict[str, Any] = {"doi": doi, "publisher_adapter": provider,
                                "rights_note": RIGHTS_NOTE, "license_code": "unverified",
                                "source_version": "publisher_api_unverified",
                                "audit_notes": []}
    errors: list[str] = []
    try:
        if provider == "springer":
            key = os.environ.get("SPRINGER_NATURE_API_KEY", "").strip()
            if not key:
                raise PermissionError("SPRINGER_NATURE_API_KEY is required for Springer OA JATS API")
            api_xml = safe_http_get(s, SPRINGER_API, params={"api_key": key, "q": f"doi:{doi}", "p": 10})
            xml = find_jats_article(api_xml, doi)
            figures = core.parse_jats(xml, include_supplementary)
            metadata["source"] = "SpringerNature_OA_JATS_API"
            article = safe_root(xml)
            metadata.update(core.jats_article_metadata(article))
            metadata["audit_notes"] = core.inspect_jats_completeness(xml, figures)
            for node in article.iter():
                for name, value in node.attrib.items():
                    if etree.QName(name).localname in {"href", "url"}:
                        node.set(name, public_reference(value))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "article.xml").write_bytes(etree.tostring(article, encoding="utf-8"))
            save_publisher_figures(figures, folder, s, provider, headers=None,
                                   include_supplementary=include_supplementary)
        elif provider == "elsevier":
            key = os.environ.get("ELSEVIER_API_KEY", "").strip()
            bearer = os.environ.get("ELSEVIER_BEARER_TOKEN", "").strip()
            inst = os.environ.get("ELSEVIER_INST_TOKEN", "").strip()
            if not key:
                raise PermissionError("ELSEVIER_API_KEY plus applicable verified content entitlement are required")
            headers = {"X-ELS-APIKey": key, "Accept": "text/xml"}
            if bearer:
                headers["Authorization"] = "Bearer " + bearer
            if inst:
                headers["X-ELS-Insttoken"] = inst
            obj_url = ELSEVIER_API + "/object/doi/" + quote(doi, safe="/")
            obj_xml = safe_http_get(s, obj_url, params={"view": "META"}, headers=headers)
            renditions = elsevier_renditions(obj_xml, doi)
            figures = group_elsevier_renditions(renditions)
            metadata["source"] = "Elsevier_Object_Retrieval_API"
            try:
                full_url = ELSEVIER_API + "/article/doi/" + quote(doi, safe="/")
                article_xml = safe_http_get(s, full_url, params={"view": "FULL"}, headers=headers)
                matched = attach_elsevier_captions(figures, article_xml, doi)
                metadata["matched_captions"] = matched
            except (OSError, ValueError, LookupError, etree.XMLSyntaxError) as exc:
                metadata["audit_notes"].append("FULL figure metadata unavailable. " + publisher_error(exc))
            if any(f.category == "unclassified" for f in figures):
                metadata["audit_notes"].append("Unclassified object references require manual verification")
            if any(f.label_source == "object_reference" for f in figures):
                metadata["audit_notes"].append("Some META objects lack a unique FULL figure locator")
            if not include_supplementary:
                figures = [f for f in figures if f.category != "supplementary"]
            save_publisher_figures(figures, folder, s, provider, headers={k: v for k, v in headers.items() if k != "Accept"},
                                   include_supplementary=include_supplementary,
                                   renditions={asset.url: asset for asset in renditions})
        else:
            raise LookupError("Science/AAAS has no verified direct figure API adapter; use PMC OA where available or supplied JATS media")
        if not figures:
            errors.append("No verifiable figure objects in official publisher response")
    except (requests.RequestException, ValueError, LookupError, PermissionError, etree.XMLSyntaxError) as exc:
        errors.append(publisher_error(exc))
    return core.write_outputs(folder, figures, metadata, errors, vor_index)


def run_one(s: requests.Session, identifier: str, output: Path, provider: str,
            include_supplementary: bool, email: str, *,
            vor_index: dict[str, dict] | None = None) -> tuple[Path, dict]:
    idn = core.normalize_identifier(identifier)
    name = core.clean_name(idn.replace("/", "_"))
    out = output / name
    selected = provider
    pmc_record = None
    if provider == "auto":
        # First try PMC as it supplies publisher OA data or the clearly labelled manuscript version.
        try:
            pmcid = core.resolve_pmcid(s, idn, email)
            pmc_record = core.select_version(s, pmcid)
            selected = "pmc"
        except (LookupError, OSError, ValueError, etree.XMLSyntaxError):
            selected = provider_for(idn) if DOI_RE.fullmatch(idn) else "unknown"
    if selected in ("pmc", "science"):
        return core.single_article(s, idn, output, include_supplementary, None, None, email,
                                   vor_index=vor_index, pmc_record=pmc_record)
    if selected in ("springer", "elsevier"):
        if not DOI_RE.fullmatch(idn):
            result = core.write_outputs(out, [], {"source": selected, "input": idn},
                                        ["Publisher adapter requires a DOI. Use --provider pmc for PMCID/PMID."], vor_index)
            return out, result
        return out, collect_publisher(s, idn, out, selected, include_supplementary, vor_index=vor_index)
    result = core.write_outputs(out, [], {"source": "unavailable", "input": idn},
                                ["No verified official publisher figure API. Try PMC OA or locally supplied JATS/media."],
                                vor_index)
    return out, result


def cli() -> int:
    ap = argparse.ArgumentParser(description="Authorized research-paper figure acquisition (PMC, Springer OA, entitled Elsevier)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--id", help="DOI, DOI URL, PMID, PMCID")
    src.add_argument("--csv", type=Path, help="CSV containing DOI/PMCID/PMID identifiers")
    src.add_argument("--local-xml", type=Path, help="Locally available JATS XML (PMC parser)")
    ap.add_argument("--local-media", type=Path, help="With --local-xml, folder of corresponding figures")
    ap.add_argument("--column", default="doi")
    ap.add_argument("--provider", default="auto", choices=("auto", "pmc", "springer", "elsevier", "science"))
    ap.add_argument("--include-supplementary", action="store_true")
    ap.add_argument("--output", type=Path, default=Path("collected_figures"))
    ap.add_argument("--email", default="", help="Optional contact address for NCBI API")
    ap.add_argument("--vor-index", type=Path, help="Independent publisher figure index JSON")
    a = ap.parse_args()
    try:
        vor_index = vor_audit.read_index(a.vor_index) if a.vor_index else None
    except (OSError, ValueError, TypeError) as exc:
        ap.error(str(exc))
    a.output.mkdir(parents=True, exist_ok=True)
    session = core.session()
    if a.local_xml:
        if not a.local_media or not a.local_xml.is_file() or not a.local_media.is_dir():
            ap.error("--local-xml requires a valid XML file and --local-media folder")
        path, stats = core.single_article(session, None, a.output, a.include_supplementary,
                                          a.local_xml, a.local_media, a.email, vor_index=vor_index)
        records = [{"identifier": str(a.local_xml), "path": str(path), "counts": stats}]
    else:
        if a.csv:
            with a.csv.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if a.column not in (reader.fieldnames or []):
                    ap.error(f"CSV missing column {a.column}")
                items = [row[a.column].strip() for row in reader if row.get(a.column, "").strip()]
        else:
            items = [a.id]
        records = []
        for ident in items:
            try:
                path, stats = run_one(session, ident, a.output, a.provider,
                                      a.include_supplementary, a.email, vor_index=vor_index)
            except (ValueError, requests.RequestException) as ex:
                path = a.output / core.clean_name(ident.replace("/", "_"))
                stats = core.write_outputs(path, [], {"input": ident}, [publisher_error(ex)], vor_index)
            records.append({"identifier": ident, "path": str(path), "counts": stats})
            print(f"{ident} expected={stats['expected_figure_records']} downloaded={stats['downloaded_media_files']} "
                  f"complete={stats['complete']} vor={stats['vor_status']} -> {path}")
    (a.output / "batch_summary.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if records and all(
        x["counts"]["complete"] and (vor_index is None or x["counts"]["vor_status"] == "matched")
        for x in records
    ) else 2


if __name__ == "__main__":
    sys.exit(cli())
