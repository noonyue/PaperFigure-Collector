#!/usr/bin/env python3
"""PaperFigure-Collector v0.3: journal figure harvesting from official PMC AWS / local JATS.

No publisher paywall bypass, no browser scraping, no PDF-to-figure guessing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from lxml import etree
from PIL import Image, UnidentifiedImageError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import ga_engine
import ga_gallery
import ga_sources
import vor_audit

BASE = "https://pmc-oa-opendata.s3.amazonaws.com"
ID_API = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
PMC_SEARCH_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
VALID_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp", ".svg", ".eps", ".pdf", ".bmp"}
FORMAT_PRIORITY = {".tif": 7, ".tiff": 7, ".svg": 6, ".eps": 5, ".png": 4, ".pdf": 3,
                   ".webp": 2, ".jpg": 2, ".jpeg": 2, ".bmp": 1, ".gif": 1}
CATEGORIES = (*vor_audit.INDEX_CATEGORIES, "unclassified")
XLINK = "{http://www.w3.org/1999/xlink}href"


def localname(element: etree._Element) -> str:
    return etree.QName(element.tag).localname if isinstance(element.tag, str) else ""


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def clean_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return value[:90] or "paper"


def fmt_image_name(ref: str) -> str:
    return Path(unquote(urlparse(ref).path)).name


def file_extension(ref: str) -> str:
    return Path(fmt_image_name(ref)).suffix.lower()


def is_yes(value: Any) -> bool:
    return str(value).lower() in {"true", "yes", "1"}


def is_graphical_abstract(element: etree._Element) -> bool:
    if localname(element) != "abstract":
        return False
    values = (element.get("abstract-type", ""), element.get("class", ""),
              child_text(element, "title"), child_text(element, "section-title"))
    return any(re.fullmatch(r"(?:graphical|graphic)(?:[-_\s]+abstract)?", value.strip(), re.I)
               for value in values)


def figure_category(fig: etree._Element) -> str:
    appendix = False
    for element in (fig, *fig.iterancestors()):
        tag = localname(element)
        if is_graphical_abstract(element):
            return "graphical_abstract"
        if tag in {"supplementary-material", "supplementary-materials", "supplement",
                   "supplementary-section", "appendices"}:
            return "supplementary"
        if tag not in {"fig", "figure", "fig-group", "figures", "sec", "section", "app",
                       "appendix", "app-group", "floats-group"}:
            continue
        label = child_text(element, "label")
        values = (label, element.get("fig-type", ""), element.get("content-type", ""),
                  element.get("sec-type", ""), element.get("class", ""))
        if tag in {"sec", "section", "app", "appendix", "app-group"}:
            values += (child_text(element, "title"), child_text(element, "section-title"))
        if any(re.match(r"^(?:extended|ext\.?)[-_\s]*data\b", value, re.I) for value in values):
            return "extended_data"
        if any(re.match(r"^(?:supplement(?:ary|al)?\b|fig(?:ure)?\.?\s*S\d+)", value, re.I)
               for value in values):
            return "supplementary"
        if tag in {"fig", "figure", "fig-group"} and any(
            re.fullmatch(r"graphical[-_\s]+abstract", value, re.I) for value in values
        ):
            return "graphical_abstract"
        appendix |= tag in {"app", "appendix", "app-group"}
    return "supplementary" if appendix else "main"


def child_text(fig: etree._Element, tag: str) -> str:
    parts = []
    for child in fig:
        if localname(child) == tag:
            parts.append(normalize_space(" ".join(child.itertext())))
    return normalize_space(" ".join(parts))


def alternative_groups(graphics: list[etree._Element]) -> list[tuple[str, list[str]]]:
    """Return preferred encoding and its fallback variants for each distinct figure asset."""
    groups: dict[Any, list[str]] = {}
    order: list[Any] = []
    for g in graphics:
        ref = g.get(XLINK) or g.get("href") or ""
        if not ref:
            continue
        alt = next((a for a in g.iterancestors() if localname(a) == "alternatives"), None)
        key = ("alternative", alt) if alt is not None else ("standalone", g)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ref)
    results: list[tuple[str, list[str]]] = []
    for key in order:
        options = sorted(groups[key], key=lambda r: FORMAT_PRIORITY.get(file_extension(r), 0), reverse=True)
        top = options[0]
        if not any(top == r[0] for r in results):
            results.append((top, options))
    return results


def choose_alternatives(graphics: list[etree._Element]) -> list[str]:
    """Select one preferred encoding per source figure asset."""
    return [selected for selected, _ in alternative_groups(graphics)]


@dataclass
class Figure:
    id: str
    label: str
    caption: str
    attribution: str
    category: str
    references: list[str]
    group_id: str = ""
    variants: dict[str, list[str]] = field(default_factory=dict)
    images: list[dict] = field(default_factory=list)
    status: str = "pending"
    label_source: str = "inferred"
    graphical_slots: tuple[ga_engine.Slot, ...] = field(default_factory=tuple, repr=False)
    graphical_report: dict | None = field(default=None, repr=False)


def jats_root(data: bytes) -> etree._Element:
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))


def jats_article_metadata(root: etree._Element) -> dict:
    # Select the primary front matter so a cited DOI or a sub-article cannot identify the article.
    meta = root.find("./{*}front/{*}article-meta")
    if meta is None:
        return {}
    article_dois = {vor_audit.normalize_doi("".join(node.itertext()))
                    for node in meta if localname(node) == "article-id" and node.get("pub-id-type", "").casefold() == "doi"}
    article_dois.discard(None)
    result = {"doi": next(iter(article_dois))} if len(article_dois) == 1 else {}
    licenses = set()
    for node in meta.findall("./{*}permissions/{*}license"):
        links = [node.get(XLINK) or node.get("href") or ""]
        links.extend("".join(child.itertext()).strip() for child in node.iter()
                     if localname(child) == "license_ref")
        for link in links:
            if re.fullmatch(r"https?://creativecommons\.org/(?:licenses/[a-z-]+|publicdomain/zero)/\d+\.\d+/?", link):
                licenses.add(link)
    if len(licenses) == 1:
        link = next(iter(licenses))
        result["license_url"] = link
        code = urlparse(link).path.split("/")[2]
        result["license_code"] = "CC0" if code == "zero" else f"CC {code.upper()}"
    return result


def figure_graphics(element: etree._Element, *, inline: bool = False, group: bool = False) -> list[etree._Element]:
    tags = {"graphic", "media", "inline-graphic"} if inline else {"graphic", "media"}
    graphics = []
    for node in element.iterdescendants():
        if localname(node) not in tags:
            continue
        for parent in node.iterancestors():
            if parent is element:
                graphics.append(node)
                break
            if localname(parent) in {"caption", "attrib", "object-id", "table-wrap", "sub-article"}:
                break
            if not group and localname(parent) in {"fig", "fig-group"}:
                break
    return graphics


def graphical_figure(discovery: ga_engine.Discovery) -> Figure | None:
    if not discovery.slots:
        return None
    owners = {slot.owner for slot in discovery.slots}
    captions = {slot.caption for slot in discovery.slots}
    references = {
        slot.variants[0].reference: [v.reference for v in slot.variants]
        for slot in discovery.slots if slot.variants
    }
    return Figure(
        id=next(iter(owners)) if len(owners) == 1 else "graphical_abstract",
        label="Graphical Abstract", caption=next(iter(captions)) if len(captions) == 1 else "",
        attribution="", category="graphical_abstract", references=list(references),
        variants=references, label_source="xml_category", graphical_slots=tuple(discovery.slots),
    )


def scan_jats(data: bytes, include_supplementary: bool = False,
              *, doi: str | None = None) -> tuple[list[Figure], ga_engine.Discovery]:
    root = jats_root(data)
    discovery = ga_sources.jats(root, doi)
    graphical = graphical_figure(discovery)
    output = [graphical] if graphical else []
    scopes = set(discovery.scopes)
    grouped = set()
    for node in root.iter():
        tag = localname(node)
        if tag not in {"fig", "fig-group"} or node in scopes:
            continue
        ancestors = list(node.iterancestors())
        ancestor_tags = {localname(parent) for parent in ancestors}
        if ancestor_tags.intersection({"ref-list", "table-wrap", "fn-group", "sub-article", "fig"}):
            continue
        if grouped.intersection(ancestors) or scopes.intersection(ancestors):
            continue
        category = figure_category(node)
        if category == "graphical_abstract":
            continue
        if "back" in ancestor_tags and category not in {"extended_data", "supplementary"}:
            continue
        if category == "supplementary" and not include_supplementary:
            continue
        label = child_text(node, "label")
        if tag == "fig-group":
            members = [el for el in node.iterdescendants() if localname(el) == "fig"]
            if not label or any(child_text(member, "label") for member in members):
                continue
            grouped.add(node)
        graphics = [g for g in figure_graphics(node, group=tag == "fig-group")
                    if g not in scopes and not scopes.intersection(g.iterancestors())]
        group = next((el for el in ancestors if localname(el) == "fig-group"), None)
        group_label = child_text(group, "label") if group is not None else ""
        label_source = "xml" if label else "xml_group" if group_label else "inferred"
        label = label or group_label
        i = len(output) + 1
        pairs = alternative_groups(graphics)
        caption = child_text(node, "caption")
        attribution = child_text(node, "attrib")
        if group is not None:
            caption = caption or child_text(group, "caption")
            attribution = attribution or child_text(group, "attrib")
        output.append(Figure(
            id=node.get("id") or f"figure_{i}", label=label or f"Figure {i}",
            caption=caption, attribution=attribution, category=category,
            references=[preferred for preferred, _ in pairs], label_source=label_source,
            group_id=group.get("id", "") if group is not None else "",
            variants=dict(pairs),
        ))
    return output, discovery


def parse_jats(data: bytes, include_supplementary: bool = False) -> list[Figure]:
    return scan_jats(data, include_supplementary)[0]


def inspect_jats_completeness(data: bytes, figures: list[Figure]) -> list[str]:
    """Flag gaps in source JATS without using XML counts as Version of Record evidence."""
    root = jats_root(data)
    counts = root.findall("./{*}front/{*}article-meta/{*}counts/{*}fig-count")
    declared = [int(el.get("count")) for el in counts if re.fullmatch(r"[0-9]{1,8}", el.get("count", ""))]
    notes = []
    if declared and max(declared) > len(figures):
        total = len(parse_jats(data, include_supplementary=True))
        if max(declared) > total:
            notes.append(f"JATS declares {max(declared)} figures but parser found {total} figure records")

    main_nums = set()
    for figure in figures:
        if figure.category == "main" and figure.label_source in vor_audit.VERIFIED_LABEL_SOURCES:
            number = re.fullmatch(r"([0-9]{1,8})[a-z]?", vor_audit.label_key("main", figure.label))
            if number:
                main_nums.add(int(number.group(1)))
    gaps = []
    previous = 0
    for current in sorted(main_nums):
        if current == previous + 2:
            gaps.append(str(previous + 1))
        elif current > previous + 2:
            gaps.append(f"{previous + 1}-{current - 1}")
        previous = current
    if gaps:
        notes.append("Gaps in main figure numbering: " + ", ".join(gaps))
    return notes


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "HEAD"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": "PaperFigure-Collector/0.3 (research figure archiving)"})
    return s


def get_bytes(s: requests.Session, url: str) -> bytes:
    with s.get(url, timeout=(10, 45), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise requests.HTTPError(f"PMC AWS returned HTTP {response.status_code}", response=response)
        data = bytearray()
        for chunk in response.iter_content(chunk_size=512 * 1024):
            data.extend(chunk)
            if len(data) > 150 * 1024 * 1024:
                raise ValueError("Asset exceeds the 150 MiB size cap")
        return bytes(data)


def normalize_identifier(identifier: str) -> str:
    value = identifier.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    if vor_audit.normalize_doi(value):
        return value
    m = re.fullmatch(r"PMC\d+", value, re.I) or re.search(r"/articles/(PMC\d+)(?:[/#?]|$)", value, re.I)
    if m:
        return (m.group(1) if m.lastindex else m.group()).upper()
    if re.match(r"^\d{4,12}$", value):
        return value
    raise ValueError("Input must be DOI, PMID, PMCID, DOI URL, or PMC article URL")


def resolve_pmcid(s: requests.Session, identifier: str, contact_email: str = "") -> str:
    norm = normalize_identifier(identifier)
    if re.fullmatch(r"PMC\d+", norm):
        return norm
    doi = vor_audit.normalize_doi(norm)
    searchable_doi = doi and '"' not in doi
    contact = {"tool": "PaperFigureCollector"}
    if contact_email:
        contact["email"] = contact_email
    try:
        response = s.get(ID_API, params={**contact, "ids": norm, "format": "json"}, timeout=25)
        response.raise_for_status()
        for record in response.json().get("records", []):
            pmcid = record.get("pmcid")
            resolved_doi = vor_audit.normalize_doi(record.get("doi"))
            if doi and resolved_doi and doi != resolved_doi:
                continue
            if pmcid and re.fullmatch(r"PMC\d+", pmcid):
                return pmcid
    except requests.RequestException:
        if not searchable_doi:
            raise

    if searchable_doi:
        # NCBI documents DOI as an exact Location ID query in PMC.
        query = {"db": "pmc", "term": f'"{doi}"[lid]', "retmode": "json", "retmax": 2}
        response = s.get(PMC_SEARCH_API, params={**contact, **query}, timeout=25)
        response.raise_for_status()
        result = response.json().get("esearchresult", {})
        ids = result.get("idlist", [])
        if str(result.get("count")) == "1" and len(ids) == 1 and re.fullmatch(r"\d+", ids[0]):
            return "PMC" + ids[0]
    raise LookupError("This identifier has no unique article match in PMC.")


def list_keys(s: requests.Session, prefix: str, delimiter: str | None = None) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    common: list[str] = []
    token = None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": 1000}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        r = s.get(BASE + "/", params=params, timeout=(10, 45))
        r.raise_for_status()
        xml = etree.fromstring(r.content, etree.XMLParser(resolve_entities=False, no_network=True))
        for el in xml.iter():
            if localname(el) == "Contents":
                keys.extend((kid.text or "") for kid in el if localname(kid) == "Key")
            if localname(el) == "CommonPrefixes":
                common.extend((kid.text or "") for kid in el if localname(kid) == "Prefix")
        is_truncated = any(localname(el) == "IsTruncated" and el.text == "true" for el in xml.iter())
        token = next((el.text for el in xml.iter() if localname(el) == "NextContinuationToken"), None)
        if not is_truncated or not token:
            break
    return keys, common


def metadata_urls(metadata: dict) -> list[str]:
    values = metadata.get("media_urls") or []
    urls = []
    for value in values:
        if isinstance(value, str):
            urls.append(value)
        if isinstance(value, dict):
            u = value.get("url") or value.get("href")
            if u:
                urls.append(u)
    return urls


def pmc_https_url(url: str) -> str | None:
    """Resolve links from PMC metadata to the public HTTPS service for the same bucket."""
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.fragment:
            return None
        host = parsed.hostname
        if not parsed.path or any(part in {".", ".."} for part in unquote(parsed.path).split("/")):
            return None
        if parsed.scheme == "s3" and parsed.netloc == "pmc-oa-opendata":
            return parsed._replace(scheme="https", netloc="pmc-oa-opendata.s3.amazonaws.com").geturl()
        if parsed.scheme == "https" and parsed.port in (None, 443) and (
            host in {"pmc-oa-opendata.s3.amazonaws.com", "pmc-oa-opendata.s3.us-east-1.amazonaws.com"}
            or (host == "s3.amazonaws.com" and parsed.path.startswith("/pmc-oa-opendata/"))
        ):
            return url
    except ValueError:
        pass
    return None


def allowed_pmc_url(url: str) -> bool:
    return pmc_https_url(url) is not None

def select_version(s: requests.Session, pmcid: str) -> tuple[str, dict, bytes]:
    _, prefixes = list_keys(s, prefix=pmcid + ".", delimiter="/")
    versions = []
    for prefix in prefixes:
        m = re.fullmatch(re.escape(pmcid) + r"\.(\d+)/", prefix)
        if not m:
            continue
        version = int(m.group(1))
        url = f"{BASE}/{prefix}{prefix.rstrip('/')}.json"
        try:
            meta = json.loads(get_bytes(s, url).decode("utf-8"))
        except (requests.RequestException, ValueError, UnicodeDecodeError):
            continue
        versions.append((version, meta))
    if not versions:
        raise LookupError(f"No accessible PMC AWS article dataset found for {pmcid}")
    # Published version first, otherwise latest available manuscript; no assumption of VoR.
    versions.sort(key=lambda item: (not is_yes(item[1].get("is_manuscript")), item[0]), reverse=True)
    version, meta = versions[0]
    prefix = f"{pmcid}.{version}"
    xml_url = pmc_https_url(meta.get("xml_url") or f"{BASE}/{prefix}/{prefix}.xml")
    if not xml_url:
        raise PermissionError("XML URL is not on the approved PMC AWS service")
    return prefix, meta, get_bytes(s, xml_url)


def match_reference(reference: str, urls: list[str]) -> str | None:
    target = fmt_image_name(reference).lower()
    if not target:
        return None
    exact = [u for u in urls if fmt_image_name(u).lower() == target]
    if exact:
        return exact[0]
    target_stem = Path(target).stem if Path(target).suffix else target
    candidates = [u for u in urls if Path(fmt_image_name(u).lower()).stem == target_stem
                  and file_extension(u) in VALID_EXT]
    if not candidates:
        return None
    return max(candidates, key=lambda u: FORMAT_PRIORITY.get(file_extension(u), 0))


def verify_image_bytes(data: bytes, ext: str) -> tuple[int | None, int | None]:
    if ext in {".svg", ".eps", ".pdf"}:
        if ext == ".svg":
            try:
                valid = localname(jats_root(data)) == "svg"
            except etree.XMLSyntaxError:
                valid = False
        else:
            signature = b"%PDF-" if ext == ".pdf" else b"%!PS"
            valid = data.startswith(signature) or (ext == ".eps" and data.startswith(b"\xc5\xd0\xd3\xc6"))
        if not valid:
            raise ValueError(f"Invalid image data ({ext})")
        return (None, None)
    try:
        from io import BytesIO
        with Image.open(BytesIO(data)) as im:
            size = im.size
            im.verify()
        return size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as ex:
        raise ValueError(f"Invalid image data ({ext}): {ex}")


def fetch_media(s: requests.Session, reference: str, remote_urls: list[str], local_media: Path | None) -> tuple[bytes, str]:
    if local_media is not None:
        filename = fmt_image_name(reference)
        candidates = list(local_media.rglob(filename))
        if not candidates:
            stem = Path(filename).stem
            candidates = [p for p in local_media.rglob("*") if p.is_file() and p.stem.lower() == stem.lower() and p.suffix.lower() in VALID_EXT]
        if not candidates:
            raise FileNotFoundError(f"No local media matches {reference}")
        p = max(candidates, key=lambda f: FORMAT_PRIORITY.get(f.suffix.lower(), 0))
        return p.read_bytes(), p.resolve().as_uri()
    url = match_reference(reference, remote_urls)
    if not url:
        raise FileNotFoundError(f"No media_urls match XML graphic '{reference}'")
    https_url = pmc_https_url(url)
    if not https_url:
        raise PermissionError("Media URL is outside the approved PMC AWS bucket")
    return get_bytes(s, https_url), https_url


def graphical_source(metadata: dict, url: str | None = None) -> ga_engine.Source:
    return ga_engine.Source(
        vor_audit.normalize_doi(metadata.get("doi")), metadata.get("source") or "unavailable", url,
        metadata.get("license_url") or metadata.get("license_code"),
        metadata.get("source_version") or "unknown", metadata.get("selected_version"),
    )


def absent_graphical_url(vor_index, doi):
    entry = (vor_index or {}).get(vor_audit.normalize_doi(doi))
    if (entry and "graphical_abstract" in entry["categories"]
            and not any(row["category"] == "graphical_abstract" for row in entry["figures"])):
        return entry["source"]["url"]
    return None


def fetch_graphical(s: requests.Session, reference: str, urls: list[str], local_media: Path | None):
    parsed = urlparse(reference)
    key = unquote(parsed.path)
    if (not key or "\\" in key or (parsed.netloc and not parsed.scheme) or
            {"..", "."}.intersection(key.split("/")) or parsed.username or parsed.password):
        raise ga_engine.AcquisitionError("unsafe_reference", "The image reference is outside the article media scope")
    name = key.removeprefix("/")
    if local_media is not None:
        root = local_media.resolve()
        if parsed.scheme or parsed.netloc or key.startswith("/") or any(c in key for c in ":*?"):
            raise ga_engine.AcquisitionError("unsafe_reference", "Local media references must use relative paths")
        exact = (root / name).resolve()
        if not exact.is_relative_to(root):
            raise ga_engine.AcquisitionError("unsafe_reference", "The image reference escapes the media directory")
        if exact.is_file():
            matches = [exact]
        elif "/" not in name:
            matches = [p.resolve() for p in root.rglob("*") if p.is_file() and p.resolve().is_relative_to(root)
                       and (p.name == name or (not Path(name).suffix and p.stem == name and p.suffix.lower() in VALID_EXT))]
        else:
            matches = []
        if len(matches) != 1:
            raise ga_engine.AcquisitionError("ambiguous_reference" if matches else "media_not_listed",
                                             "No unique authorized local image for the exact reference")
        source = matches[0]
        if source.stat().st_size > ga_engine.MAX_BYTES:
            raise ga_engine.AcquisitionError("invalid_size", "The source image exceeds 80 MiB")
        return source.read_bytes(), source.as_uri()
    verified = [u for raw in urls if (u := pmc_https_url(raw))]
    if parsed.scheme:
        wanted = pmc_https_url(reference)
        matches = [u for u in verified if wanted and urlparse(u)._replace(query="") == urlparse(wanted)._replace(query="")]
    else:
        matches = [u for u in verified if unquote(urlparse(u).path).endswith("/" + name)]
        if not matches and not Path(name).suffix:
            matches = [u for u in verified if file_extension(u) in VALID_EXT and
                       unquote(urlparse(u).path).rsplit(".", 1)[0].endswith("/" + name)]
    matches = list(dict.fromkeys(matches))
    if len(matches) != 1:
        raise ga_engine.AcquisitionError("ambiguous_reference" if matches else "media_not_listed",
                                         "No unique PMC media entry for the exact graphical abstract reference")
    return get_bytes(s, matches[0]), matches[0]


def attach_graphical_result(fig: Figure, report: dict) -> None:
    fig.graphical_report = report
    for row in report["records"]:
        asset = row["asset"]
        entry = {"reference": row["reference"], "caption": row["caption"], "attribution": row["attribution"],
                 "edition": row["edition"], "error": row["reason"], "attempts": row["attempts"],
                 "status": "downloaded" if asset else "missing", "file": None}
        fig.images.append({**entry, **(asset or {})})
    fig.references = [ga_engine.clean_reference(ref) or "" for ref in fig.references]
    fig.variants = {ga_engine.clean_reference(ref): [ga_engine.clean_reference(url) for url in variants]
                    for ref, variants in fig.variants.items()}
    saved = sum(a["status"] == "downloaded" for a in fig.images)
    fig.status = "downloaded" if saved and saved == len(fig.images) else "partial" if saved else "missing"


def collect_figures(figures: list[Figure], article_dir: Path, s: requests.Session,
                    remote_urls: list[str], local_media: Path | None, *,
                    graphical: ga_engine.Discovery | None = None,
                    ga_source: ga_engine.Source | None = None,
                    ga_access: bool = True, absence_url: str | None = None) -> dict:
    if graphical is None:
        graphical = ga_engine.Discovery(
            slots=[slot for fig in figures for slot in fig.graphical_slots], scanned=True,
        )

    def fetch(variant):
        if not ga_access:
            raise ga_engine.AcquisitionError("outside_open_access_subset",
                                             "PMC records this version outside the authorized Open Access Subset")
        return fetch_graphical(s, variant.reference, remote_urls, local_media)

    ga_report = ga_engine.acquire(graphical, ga_source or ga_engine.Source(None, "local_jats"),
                                  article_dir, fetch, absence_url=absence_url)
    for ix, fig in enumerate(figures, 1):
        if fig.graphical_slots:
            attach_graphical_result(fig, ga_report)
            continue
        dest = article_dir / fig.category
        dest.mkdir(parents=True, exist_ok=True)
        for pos, ref in enumerate(fig.references, 1):
            entry: dict[str, Any] = {"reference": ref, "status": "missing", "source_url": None,
                                    "file": None, "sha256": None, "width": None, "height": None, "error": None}
            try:
                # Prefer the highest-quality original that is actually available;
                # if e.g. print-only TIFF is absent, fall back to the web image.
                attempts = []
                for candidate in fig.variants.get(ref, [ref]):
                    try:
                        data, src = fetch_media(s, candidate, remote_urls, local_media)
                        ext = file_extension(src) or file_extension(candidate)
                        if ext not in VALID_EXT:
                            raise ValueError("Unrecognized figure file extension")
                        width, height = verify_image_bytes(data, ext)
                        break
                    except (OSError, ValueError, requests.RequestException, PermissionError) as exc:
                        attempts.append(f"{candidate}: {exc}")
                else:
                    raise FileNotFoundError(". ".join(attempts) or f"No matching media: {ref}")
                name = f"{ix:03d}_{clean_name(fig.id)}_{pos:02d}{ext}"
                path = dest / name
                path.write_bytes(data)
                entry.update({"status": "downloaded", "source_url": src,
                              "file": str(path.relative_to(article_dir)).replace('\\', '/'),
                              "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height,
                              "quality_tier": "source_resolution_unverified",
                              "fallback_variant": candidate != ref})
            except (OSError, ValueError, requests.RequestException, PermissionError) as ex:
                entry["error"] = str(ex)
            fig.images.append(entry)
            if local_media is None:
                time.sleep(0.12)
        fig.status = ("downloaded" if fig.images and all(a["status"] == "downloaded" for a in fig.images)
                      else "partial" if any(a["status"] == "downloaded" for a in fig.images)
                      else "missing")
    return ga_report


def write_outputs(article_dir: Path, figures: list[Figure], metadata: dict, errors: list[str],
                  vor_index: dict[str, dict] | None = None, *, graphical: dict | None = None) -> dict:
    article_dir.mkdir(parents=True, exist_ok=True)
    counts = {cat: sum(f.category == cat for f in figures) for cat in CATEGORIES}
    expected = len(figures)
    full = sum(f.status == "downloaded" for f in figures)
    partial = sum(f.status == "partial" for f in figures)
    missing = sum(f.status == "missing" for f in figures)
    file_count = len({a["file"] for f in figures for a in f.images
                      if a["status"] == "downloaded" and a.get("file")})
    stats = {"expected_figure_records": expected, "complete_figure_records": full,
             "partial_figure_records": partial, "missing_figure_records": missing,
             "downloaded_media_files": file_count, "categories": counts,
             "complete": not errors and not metadata.get("audit_notes") and expected > 0 and full == expected}
    if graphical is None:
        graphical = next((f.graphical_report for f in figures if f.graphical_report), None)
    if graphical is None:
        graphical = ga_engine.acquire(
            ga_engine.Discovery(reason="; ".join(errors) or "The source could not be scoped for graphical abstracts"),
            graphical_source(metadata), article_dir, lambda _: (b"", ""),
            absence_url=absent_graphical_url(vor_index, metadata.get("doi")),
        )
    figure_rows = [{k: v for k, v in asdict(f).items() if k not in {"graphical_slots", "graphical_report"}}
                   for f in figures]
    payload = {"metadata": metadata, "counts": stats, "errors": errors,
               "figures": figure_rows, "graphical_abstract": graphical}
    (article_dir / "graphical_abstract.json").write_text(json.dumps(graphical, ensure_ascii=False, indent=2), encoding="utf-8")
    comparison = vor_audit.compare(payload, vor_index, article_dir)
    payload["vor_audit"] = comparison
    stats["vor_status"] = comparison["status"]
    (article_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with (article_dir / "figure_index.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["category", "id", "group_id", "label", "status", "caption",
                                                    "attribution", "file", "source_url", "sha256", "width", "height", "quality_tier", "error",
                                                    "format", "dimension_unit", "edition", "source_version", "acquisition_method", "doi", "license"])
        writer.writeheader()
        for fig in figures:
            for asset in fig.images or [{}]:
                writer.writerow({"category": fig.category, "id": fig.id, "group_id": fig.group_id,
                                 "label": fig.label, "caption": asset.get("caption", fig.caption),
                                 "attribution": asset.get("attribution", fig.attribution),
                                 "status": asset.get("status", fig.status), "file": asset.get("file"),
                                 "source_url": asset.get("source_url"), "sha256": asset.get("sha256"),
                                 "width": asset.get("width"), "height": asset.get("height"),
                                 "quality_tier": asset.get("quality_tier", "source_resolution_unverified"),
                                 "error": asset.get("error") or ("No graphic asset in XML" if not fig.references else ""),
                                 **{key: asset.get(key) for key in ("format", "dimension_unit", "edition",
                                                                   "source_version", "acquisition_method", "doi", "license")}})

    lines = ["# Figure captions from the acquisition source", "",
             f"Source: {metadata.get('source', 'unknown')}", f"DOI: {metadata.get('doi', 'unknown')}", ""]
    for fig in figures:
        lines += [f"## {fig.label} [{fig.category}]", ""]
        if fig.graphical_slots:
            for row in (fig.graphical_report or graphical)["records"]:
                lines += [f"### {row['edition']} · {row['id']}", "", row["caption"] or "(Caption unavailable in source)", ""]
        else:
            lines += [fig.caption or "(Caption unavailable in source)", ""]
        if fig.attribution:
            lines += ["Attribution: " + fig.attribution, ""]
    (article_dir / "captions.md").write_text("\n".join(lines), encoding="utf-8")

    ga_gallery.render(article_dir / "gallery.html", [(article_dir, payload)])

    audit = ["# Figure extraction audit", "", "## Summary", "",
             f"- Figure records in acquisition source: **{expected}**",
             f"- Complete records: **{full}**", f"- Partial records: **{partial}**",
             f"- Missing records: **{missing}**", f"- Downloaded media files: **{file_count}**",
             f"- Acquisition complete within source: **{stats['complete']}**", "",
             "## Version of Record comparison", "",
             f"- Status **{comparison['status']}**",
             f"- Acquisition version **{comparison['source_version']}**"]
    if comparison["status"] == "not_checked":
        audit.append(f"- Reason {comparison['reason']}")
    else:
        ref = comparison["reference"]
        totals = comparison["counts"]
        audit.extend([
            f"- Reference checked {ref['checked_on']} ([{ref['kind']}]({ref['url']}))",
            f"- Scope {', '.join(comparison['categories'])}",
            f"- Verified labels {totals['matched_labels']}/{totals['expected']}",
            f"- Figures with verified archived files {totals['verified_figures']}/{totals['expected']}",
            f"- Author manuscript {comparison['version_status'] == 'author_manuscript'}",
        ])
        for row in comparison["figures"]:
            if row["status"] != "matched":
                audit.append(f"- {row['category']} {row['label']} — {row['status']}")
            for asset in row.get("assets", []):
                if asset["status"] != "verified":
                    detail = asset.get("error") or asset["status"]
                    audit.append(f"  - {asset.get('file') or 'Asset'} — {detail}")
            if row.get("lower_resolution"):
                audit.append(f"- {row['label']} includes a lower-resolution rendition")
        for row in comparison["unexpected_records"]:
            audit.append(f"- Extra record {row['category']} {row['label']} ({row['id']})")
        for row in comparison["unresolved_records"]:
            audit.append(f"- Unresolved label {row['category']} {row['label']} ({row['label_source']})")
    audit.extend(["", "The comparison checks labels within the listed categories and the integrity of archived files.",
                  "Publication rendition identity requires separate evidence.", ""])
    if metadata.get("audit_notes"):
        audit += ["## Needs review", ""] + [f"- {v}" for v in metadata["audit_notes"]] + [""]
    if errors:
        audit += ["## Collection errors", ""] + [f"- {e}" for e in errors] + [""]
    audit.extend(["", "## Graphical Abstract", "", f"- Status {graphical['status']}"])
    if graphical["reason"]:
        audit.append(f"- {graphical['reason']}")
    audit.extend(f"- {r['edition']} · {r['status']} · {r['reason'] or r['evidence']}" for r in graphical["records"])
    incomplete = [f for f in figures if f.status != "downloaded"]
    if incomplete:
        audit += ["## Missing / partial figures", ""]
        for fig in incomplete:
            audit.append(f"- {fig.label} [{fig.category}]: {fig.status}; " +
                         "; ".join(a.get("error") or "no matched file" for a in fig.images))
    audit += ["", "## Copyright", "", "Recorded license: " + str(metadata.get("license_code") or "unverified"),
              "Reuse is subject to the relevant article license and figure-level credits."]
    (article_dir / "audit.md").write_text("\n".join(audit), encoding="utf-8")
    return stats


def single_article(s: requests.Session, identifier: str | None, base_output: Path,
                   include_supplementary: bool, local_xml: Path | None, local_media: Path | None,
                   email: str = "", *, vor_index: dict[str, dict] | None = None,
                   pmc_record: tuple[str, dict, bytes] | None = None) -> tuple[Path, dict]:
    folder_name = clean_name(local_xml.stem if local_xml else (identifier or "unknown").replace("/", "_"))
    article_dir = base_output / folder_name
    figures: list[Figure] = []
    errors: list[str] = []
    metadata: dict[str, Any] = {"input": identifier or str(local_xml), "source": "local_jats" if local_xml else "PMC_AWS_2026"}
    remote_urls: list[str] = []
    ga_report = None
    ga_access = True
    source_url = None
    try:
        if local_xml:
            xml = local_xml.read_bytes()
            if local_media is None:
                raise ValueError("Local JATS mode requires --local-media directory")
            metadata.update(jats_article_metadata(jats_root(xml)))
            metadata["source_version"] = "local_jats_unverified"
            source_url = local_xml.resolve().as_uri()
        else:
            if pmc_record is None:
                pmcid = resolve_pmcid(s, identifier or "", email)
                pmc_record = select_version(s, pmcid)
            version, meta, xml = pmc_record
            xml_metadata = jats_article_metadata(jats_root(xml))
            asserted_dois = {vor_audit.normalize_doi(value) for value in
                             (identifier, meta.get("doi"), xml_metadata.get("doi"))}
            asserted_dois.discard(None)
            if len(asserted_dois) > 1:
                raise LookupError("PMC article DOI disagrees with the requested DOI or JATS primary DOI")
            metadata.update({k: meta.get(k) for k in ["pmcid", "doi", "title", "citation", "license_code",
                                                      "is_pmc_openaccess", "is_manuscript", "is_retracted"]})
            metadata["doi"] = metadata["doi"] or xml_metadata.get("doi")
            if xml_metadata.get("license_url"):
                metadata["license_url"] = xml_metadata["license_url"]
            metadata["selected_version"] = version
            source_url = pmc_https_url(meta.get("xml_url") or f"{BASE}/{version}/{version}.xml")
            metadata["source_version"] = "author_manuscript" if is_yes(meta.get("is_manuscript")) else "published_in_pmc"
            if is_yes(meta.get("is_pmc_openaccess")) and str(meta.get("license_code", "")).upper() != "TDM":
                remote_urls = metadata_urls(meta)
            else:
                errors.append("PMC dataset marks this version as TDM-only or outside the Open Access Subset")
                ga_access = False
        figures, discovery = scan_jats(xml, include_supplementary, doi=metadata.get("doi"))
        metadata["audit_notes"] = inspect_jats_completeness(xml, figures)
        if not figures:
            errors.append("No figure records found in source JATS")
        ga_report = collect_figures(
            figures, article_dir, s, remote_urls, local_media, graphical=discovery,
            ga_source=graphical_source(metadata, source_url), ga_access=ga_access,
            absence_url=absent_graphical_url(vor_index, metadata.get("doi")),
        )
    except requests.RequestException as ex:
        detail = f"HTTP {ex.response.status_code}" if ex.response is not None else type(ex).__name__
        errors.append(f"PMC request failed ({detail})")
    except (OSError, ValueError, LookupError, etree.XMLSyntaxError) as ex:
        errors.append(f"{type(ex).__name__}: {ex}")
    stats = write_outputs(article_dir, figures, metadata, errors, vor_index, graphical=ga_report)
    return article_dir, stats


def cli() -> int:
    p = argparse.ArgumentParser(description="Archive article figures from official PMC AWS or user-supplied JATS")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--id", help="DOI / PMID / PMCID / DOI URL")
    source.add_argument("--csv", type=Path, help="CSV of identifiers")
    source.add_argument("--local-xml", type=Path, help="Local JATS article XML")
    p.add_argument("--local-media", type=Path, help="Directory of original media to use with --local-xml")
    p.add_argument("--column", default="doi", help="CSV column, default doi")
    p.add_argument("--include-supplementary", action="store_true")
    p.add_argument("--output", type=Path, default=Path("collected_figures"))
    p.add_argument("--email", default="", help="Contact email for NCBI ID Converter (recommended)")
    p.add_argument("--vor-index", type=Path, help="Independent publisher figure index JSON")
    args = p.parse_args()
    try:
        vor_index = vor_audit.read_index(args.vor_index) if args.vor_index else None
    except (OSError, ValueError, TypeError) as exc:
        p.error(str(exc))
    if args.local_xml and (not args.local_xml.is_file() or not args.local_media or not args.local_media.is_dir()):
        p.error("--local-xml requires an existing XML file and --local-media an existing directory")
    identifiers = []
    if args.csv:
        with args.csv.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if args.column not in (reader.fieldnames or []):
                p.error(f"CSV is missing column {args.column!r}")
            identifiers = [row[args.column].strip() for row in reader if row.get(args.column, "").strip()]
    elif args.id:
        identifiers = [args.id]
    elif args.local_xml:
        identifiers = [None]
    args.output.mkdir(parents=True, exist_ok=True)
    s = session()
    results = []
    for identifier in identifiers:
        path, counts = single_article(s, identifier, args.output, args.include_supplementary,
                                      args.local_xml, args.local_media, args.email, vor_index=vor_index)
        results.append({"identifier": identifier or str(args.local_xml), "path": str(path), "counts": counts})
        print(f"{identifier or args.local_xml}: expected={counts['expected_figure_records']} complete={counts['complete_figure_records']} missing={counts['missing_figure_records']} files={counts['downloaded_media_files']} -> {path}")
    (args.output / "batch_summary.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if results and all(
        r["counts"]["complete"] and (vor_index is None or r["counts"]["vor_status"] == "matched")
        for r in results
    ) else 2


if __name__ == "__main__":
    sys.exit(cli())
