"""Publisher-specific graphical abstract evidence and explicit object associations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import re
from urllib.parse import unquote, urljoin, urlsplit

from lxml import etree, html

from ga_engine import Candidate, Discovery, Slot, Variant
from vor_audit import normalize_doi

XLINK = "{http://www.w3.org/1999/xlink}href"
CE = "http://www.elsevier.com/xml/common/dtd"
FIG = {"fig", "figure", "fig-group"}
VISUAL = {"graphic", "inline-graphic", "media"}
BARRIERS = {
    "caption",
    "figcaption",
    "legend",
    "attrib",
    "object-id",
    "table",
    "table-wrap",
    "sub-article",
    "ref-list",
    "bibliography",
    "supplementary-material",
    "supplement",
    "fn-group",
}
ROLE = re.compile(r"(?:graphical|graphic|visual)(?:[-_\s]+abstract)?", re.I)
NORMAL_LABEL = re.compile(r"fig(?:ure)?\.?\s*\d+[a-z]?[.:]?", re.I)
EDITIONS = {
    "web-only": "web_only",
    "online-only": "web_only",
    "print-only": "print_only",
}
ELSEVIER_RANK = {"IMAGE-HIGH-RES": 3, "IMAGE-DOWNSAMPLED": 2, "IMAGE-THUMBNAIL": 1}


def tag(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def text(node):
    return " ".join(" ".join(node.itertext()).split())


def child(node, name):
    return " ".join(text(c) for c in node if tag(c) == name)


def role(value):
    return bool(ROLE.fullmatch(value.strip()))


def xml(data):
    return etree.fromstring(
        data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    )


def provider(doi):
    value = (normalize_doi(doi) or "").split("/")[0]
    if value in {"10.1038", "10.1007", "10.1186", "10.1057", "10.1245"}:
        return "springer"
    if value in {"10.1016", "10.1177", "10.1053", "10.1067"}:
        return "elsevier" if value != "10.1177" else "jats"
    if value == "10.1080":
        return "taylor_francis"
    return "jats"


def article_doi(node):
    meta = node.find("./{*}front/{*}article-meta")
    if meta is None:
        return None
    values = {
        normalize_doi(text(n))
        for n in meta
        if tag(n) == "article-id" and n.get("pub-id-type", "").lower() == "doi"
    }
    values.discard(None)
    if len(values) > 1:
        raise LookupError("The primary JATS article declares conflicting DOI values")
    return next(iter(values), None)


def no_barrier(node, scope):
    for parent in node.iterancestors():
        if parent is scope:
            return True
        if tag(parent) in BARRIERS:
            return False
    return False


def primary_abstract(node):
    return any(
        tag(a) == "abstract"
        and a.getparent() is not None
        and tag(a.getparent()) == "article-meta"
        for a in node.iterancestors()
    )


def edition(nodes):
    flags = {
        EDITIONS[value]
        for node in nodes
        for name in ("specific-use", "content-type", "class", "use", "edition")
        for value in re.split(r"[\s,;]+", node.get(name, "").lower().replace("_", "-"))
        if value in EDITIONS
    }
    if len(flags) > 1:
        return None
    return next(iter(flags), "unspecified")


def marker(node):
    if tag(node) in {"abstract", *FIG, "sec", "boxed-text", *VISUAL}:
        return any(
            role(v)
            for v in (
                node.get("abstract-type", ""),
                node.get("class", ""),
                node.get("fig-type", ""),
                node.get("content-type", ""),
                node.get("sec-type", ""),
                node.get("role", ""),
                node.get("type", ""),
                child(node, "title"),
                child(node, "label"),
                child(node, "section-title"),
            )
        ) or any(role(item) for item in node.get("class", "").split())
    return False


class JATSAdapter:
    name = "jats"

    def assume_role(self, node):
        return False

    def read(self, article):
        marks = [
            n
            for n in article.iterdescendants()
            if no_barrier(n, article) and (marker(n) or self.assume_role(n))
        ]
        found = regions(article, marks, VISUAL, self.name)
        scopes = set(found.scopes)
        if scopes:
            ordinary = set()
            for figure in article.iterdescendants():
                if (
                    tag(figure) not in FIG
                    or figure in scopes
                    or scopes.intersection(figure.iterancestors())
                ):
                    continue
                for graphic in figure.iterdescendants():
                    if (
                        tag(graphic) in VISUAL
                        and graphic not in scopes
                        and not scopes.intersection(graphic.iterancestors())
                        and no_barrier(graphic, figure)
                    ):
                        reference = graphic.get(XLINK) or graphic.get("href")
                        if reference:
                            ordinary.add(reference)
            retained = []
            for slot in found.slots:
                remaining = tuple(
                    v for v in slot.variants if v.reference not in ordinary
                )
                for v in slot.variants:
                    if v.reference in ordinary:
                        found.candidates.append(
                            Candidate(
                                v.reference,
                                "ambiguous_locator",
                                "The XML reference is also assigned to an ordinary figure",
                            )
                        )
                if remaining or not slot.variants:
                    retained.append(replace(slot, variants=remaining))
            found.slots = retained
        for abstract in article.findall("./{*}front/{*}article-meta/{*}abstract"):
            if any(
                scope is abstract or abstract in scope.iterancestors()
                for scope in found.scopes
            ):
                continue
            for n in abstract.iterdescendants():
                if tag(n) not in VISUAL or not no_barrier(n, abstract):
                    continue
                href = n.get(XLINK) or n.get("href")
                if href:
                    found.candidates.append(
                        Candidate(
                            href,
                            "unlabelled_abstract_image",
                            "The primary abstract contains media without an explicit graphical-abstract role",
                        )
                    )
        return found


class SpringerNatureAdapter(JATSAdapter):
    name = "springer_jats"

    def assume_role(self, node):
        # Springer production JATS reserves Figa as an anchored abstract figure.
        return (
            tag(node) == "fig"
            and node.get("id") == "Figa"
            and node.get("position") == "anchor"
            and primary_abstract(node)
            and not child(node, "label")
        )


class TaylorFrancisAdapter(JATSAdapter):
    name = "taylor_francis_jats"


class ElsevierJATSAdapter(JATSAdapter):
    name = "elsevier_jats"


def regions(root, marks, visual, adapter):
    mark_set = set(marks)
    # Nested explicit cues reinforce the same region and keep labels close to their own panels.
    scopes = tuple(
        n for n in marks if not any(a in mark_set for a in n.iterancestors())
    )
    result = Discovery(scanned=True, scopes=scopes)
    groups = {}
    for scope in scopes:
        present = False
        all_nodes = (scope, *scope.iterdescendants())
        for node in all_nodes:
            if tag(node) not in visual or (
                node is not scope and not no_barrier(node, scope)
            ):
                continue
            if tag(node) == "media":
                mime = (node.get("mimetype") or node.get("mime-type") or "").casefold()
                if mime and mime != "image" and not mime.startswith("image/"):
                    continue
            ref = node.get("locator") if tag(node) == "link" else None
            ref = (ref or node.get(XLINK) or node.get("href") or "").strip()
            if not ref:
                continue
            present = True
            within = [node]
            if node is not scope:
                for a in node.iterancestors():
                    within.append(a)
                    if a is scope:
                        break
            selected_edition = edition(within)
            if selected_edition is None:
                result.candidates.append(
                    Candidate(
                        ref,
                        "conflicting_rendition",
                        "Conflicting web-only and print-only declarations",
                    )
                )
                continue
            carrier = next((a for a in within if tag(a) in FIG), scope)
            label = child(carrier, "label")
            if NORMAL_LABEL.fullmatch(label):
                result.candidates.append(
                    Candidate(
                        ref,
                        "conflicting_figure_label",
                        "A graphical region labels this object as an ordinary numbered figure",
                    )
                )
                continue
            alt = next((a for a in within if tag(a) == "alternatives"), node)
            # Figure- or variant-level captions take precedence over the owning abstract's caption.
            caption = next(
                (child(a, "caption") for a in within if child(a, "caption")), ""
            )
            attribution = next(
                (child(a, "attrib") for a in within if child(a, "attrib")), ""
            )
            anchor = carrier.get("id") or scope.get("id") or "graphical_abstract"
            key = (scope, carrier, alt, selected_edition, caption, attribution)
            if key not in groups:
                path = root.getroottree().getpath(carrier)
                groups[key] = [anchor, selected_edition, caption, attribution, path, []]
            if ref not in groups[key][-1]:
                groups[key][-1].append(ref)
        if not present:
            key = (scope, scope, scope, edition([scope]) or "unspecified", "", "")
            groups[key] = [
                scope.get("id") or "graphical_abstract",
                key[3],
                child(scope, "caption"),
                child(scope, "attrib"),
                root.getroottree().getpath(scope),
                [],
            ]
    for i, (owner, chosen, caption, credit, path, refs) in enumerate(
        groups.values(), 1
    ):
        result.slots.append(
            Slot(
                f"ga_{i:03d}",
                owner,
                chosen,
                tuple(Variant(ref) for ref in refs),
                caption,
                credit,
                adapter,
                path,
            )
        )
    return result


def jats(data, doi=None, *, publisher=None):
    node = xml(data) if isinstance(data, bytes) else data
    current = article_doi(node)
    expected = normalize_doi(doi)
    if tag(node) != "article":
        matches = [
            x
            for x in node.iterdescendants()
            if tag(x) == "article"
            and expected is not None
            and article_doi(x) == expected
        ]
        if len(matches) != 1:
            raise LookupError(
                "No unique primary JATS article matches the requested DOI"
            )
        node = matches[0]
        current = article_doi(node)
    if expected and current and expected != current:
        raise LookupError("JATS DOI disagrees with the requested DOI")
    route = publisher or provider(expected or current)
    adapter = {
        "springer": SpringerNatureAdapter,
        "taylor_francis": TaylorFrancisAdapter,
        "elsevier": ElsevierJATSAdapter,
    }.get(route, JATSAdapter)
    return adapter().read(node)


def elsevier(data, objects, doi):
    """Bind exact CE or JATS FULL locators to entitled META renditions."""
    by_ref = defaultdict(list)
    for obj in objects:
        by_ref[obj.ref].append(obj)
    for bucket in by_ref.values():
        bucket.sort(key=lambda item: ELSEVIER_RANK.get(item.tier, 0), reverse=True)
    if data is None:
        found = Discovery(
            reason="Elsevier FULL structured figure roles were unavailable"
        )
    else:
        root = xml(data)
        if tag(root) == "full-text-retrieval-response":
            coredata = root.find("./{*}coredata")
            declared = (
                normalize_doi(child(coredata, "doi")) if coredata is not None else None
            )
            if declared and declared != normalize_doi(doi):
                raise LookupError("Elsevier FULL DOI disagrees with the requested DOI")
            original = root.find("./{*}originalText")
            node = (
                next((n for n in original if tag(n) in {"doc", "article"}), None)
                if original is not None
                else None
            )
            if node is None:
                raise LookupError(
                    "Elsevier FULL response lacks structured originalText"
                )
            root = node
        if tag(root) == "article":
            found = jats(root, doi, publisher="elsevier")
        elif tag(root) == "doc":
            meta = root.find("./{*}meta")
            declared = normalize_doi(child(meta, "doi")) if meta is not None else None
            if declared and declared != normalize_doi(doi):
                raise LookupError("Elsevier FULL DOI disagrees with the requested DOI")
            marks = [
                n
                for n in root.iterdescendants()
                if isinstance(n.tag, str)
                and etree.QName(n).namespace == CE
                and marker(n)
                and no_barrier(n, root)
            ]
            found = regions(root, marks, VISUAL | {"link"}, "elsevier_full_ce")
        else:
            raise LookupError("Elsevier FULL response lacks structured article XML")
        displaced = set(found.scopes)
        ordinary_refs = set()
        for fig in root.iterdescendants():
            if (
                tag(fig) not in FIG
                or fig in displaced
                or any(a in displaced for a in fig.iterancestors())
            ):
                continue
            for n in fig.iterdescendants():
                if tag(n) in VISUAL | {"link"} and no_barrier(n, fig):
                    value = n.get("locator") or n.get(XLINK) or n.get("href") or ""
                    ordinary_refs.add(value.removeprefix("#"))
        bindings = []
        for slot in found.slots:
            ref_groups = defaultdict(list)
            for v in slot.variants:
                ref = v.reference.removeprefix("#")
                if ref in ordinary_refs:
                    found.candidates.append(
                        Candidate(
                            ref,
                            "ambiguous_locator",
                            "The graphical abstract and an ordinary figure point to the same FULL object locator",
                        )
                    )
                    continue
                options = by_ref.get(ref, ())
                if options:
                    for obj in options:
                        variant_edition = getattr(obj, "edition", "unspecified")
                        if slot.edition != "unspecified" and variant_edition not in {
                            "unspecified",
                            slot.edition,
                        }:
                            continue
                        key = (
                            variant_edition
                            if slot.edition == "unspecified"
                            else slot.edition
                        )
                        ref_groups[key].append(
                            Variant(obj.url, obj.tier, obj.width, obj.height)
                        )
                else:
                    ref_groups[slot.edition].append(v)
            if not slot.variants:
                bindings.append(slot)
            elif not ref_groups and not any(
                v.reference.removeprefix("#") in ordinary_refs for v in slot.variants
            ):
                bindings.append(replace(slot, variants=()))
            for selected, variants in ref_groups.items():
                owner = slot.owner
                if (
                    owner == "graphical_abstract"
                    and len(slot.variants) == 1
                    and slot.variants[0].reference in by_ref
                ):
                    owner = slot.variants[0].reference
                bindings.append(
                    replace(
                        slot,
                        id=f"{slot.id}_{selected}",
                        owner=owner,
                        edition=selected,
                        variants=tuple(dict.fromkeys(variants)),
                    )
                )
        found.slots = bindings
    bound = {v.reference for s in found.slots for v in s.variants}
    added = set()
    for ref, bucket in by_ref.items():
        for obj in bucket:
            if obj.url in bound:
                continue
            key = (ref, getattr(obj, "edition", "unspecified"))
            if (
                getattr(obj, "graphical_role", False)
                and (data is None or ref not in ordinary_refs)
                and key not in added
            ):
                variants = [
                    Variant(o.url, o.tier, o.width, o.height)
                    for o in bucket
                    if getattr(o, "graphical_role", False)
                    and getattr(o, "edition", "unspecified") == key[1]
                ]
                found.slots.append(
                    Slot(
                        f"ga_meta_{len(found.slots) + 1:03d}",
                        ref,
                        key[1],
                        tuple(variants),
                        evidence="elsevier_meta_explicit_role",
                        locator=ref,
                    )
                )
                added.add(key)
            elif data is None and not getattr(obj, "graphical_role", False):
                found.candidates.append(
                    Candidate(
                        ref,
                        "unverified_object_role",
                        "META alone does not identify the object as a graphical abstract",
                    )
                )
                break
    return found


def https_host(url):
    try:
        u = urlsplit(url)
        decoded = unquote(u.path)
        if (
            u.scheme == "https"
            and u.hostname
            and not u.username
            and not u.password
            and u.port in (None, 443)
            and not re.search(r"[\x00-\x20\\]", decoded)
            and not {"..", "."} & set(decoded.split("/"))
        ):
            return u.hostname
    except ValueError:
        pass
    return None


ARTICLE_HOSTS = {
    "springer": {
        "www.nature.com",
        "nature.com",
        "link.springer.com",
        "www.biomedcentral.com",
    },
    "elsevier": {"www.sciencedirect.com", "www.cell.com"},
    "taylor_francis": {"www.tandfonline.com", "tandfonline.com"},
}
IMAGE_HOSTS = {
    "springer": {
        "media.springernature.com",
        "static-content.springer.com",
        "www.nature.com",
        "link.springer.com",
    },
    "elsevier": {
        "ars.els-cdn.com",
        "www.sciencedirect.com",
        "www.cell.com",
        "www.elsevier.com",
    },
    "taylor_francis": {
        "www.tandfonline.com",
        "tandfonline.com",
        "images.tandf.co.uk",
        "files.taylorandfrancis.com",
    },
}
HTML_SCOPES = {
    "springer": '//*[@data-title="Graphical abstract" or @data-title="Graphical Abstract" or @data-test="graphical-abstract" or @id="graphical-abstract"]',
    "elsevier": '//*[contains(concat(" ",normalize-space(@class)," ")," graphical ") and contains(concat(" ",normalize-space(@class)," ")," abstract ") or @id="graphical-abstract" or @data-abstract-type="graphical"]',
    "taylor_francis": '//*[contains(concat(" ",normalize-space(@class)," ")," abstract ") and contains(concat(" ",normalize-space(@class)," ")," graphical ") or contains(concat(" ",normalize-space(@class)," ")," graphicalAbstract ") or contains(concat(" ",normalize-space(@class)," ")," graphical-abstract ") or @data-abstract-type="graphical"]',
}


def publisher_html(data, url, doi):
    host = https_host(url)
    adapter = next((p for p, hosts in ARTICLE_HOSTS.items() if host in hosts), None)
    if adapter is None:
        raise PermissionError(
            "The article URL is outside the supported publisher pages"
        )
    doc = html.fromstring(data, parser=html.HTMLParser(no_network=True))
    actual = {
        normalize_doi(x)
        for x in doc.xpath(
            '//meta[translate(@name,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="citation_doi"]/@content'
        )
    }
    actual.discard(None)
    if actual != {normalize_doi(doi)}:
        raise LookupError(
            "Publisher HTML citation DOI does not uniquely identify the requested article"
        )
    scopes = list(doc.xpath(HTML_SCOPES[adapter]))
    for heading in doc.xpath("//h1|//h2|//h3|//h4"):
        parent = heading.getparent()
        if (
            re.fullmatch(r"(?:graphical|visual)\s+abstract[:.]?", text(heading), re.I)
            and tag(parent) in {"section", "div", "aside"}
            and len(parent.xpath("./h1|./h2|./h3|./h4")) == 1
        ):
            scopes.append(parent)
    marks = set(scopes)
    scopes = tuple(
        n
        for n in dict.fromkeys(scopes)
        if not any(a in marks for a in n.iterancestors())
    )
    result = Discovery(scanned=True)
    for meta in doc.xpath(
        '//meta[@property="og:image" or @name="twitter:image"]/@content'
    )[:8]:
        result.candidates.append(
            Candidate(
                urljoin(url, meta), "social_image", "Open Graph or social-card image"
            )
        )
    for i, n in enumerate(doc.xpath("//img[not(ancestor::figcaption)]")):
        ref = n.get("data-src") or n.get("src")
        if not ref:
            continue
        reference = urljoin(url, ref)
        in_scope = next((a for a in n.iterancestors() if a in scopes), None)
        figure = next((a for a in n.iterancestors() if tag(a) == "figure"), None)
        legend = child(figure, "figcaption") if figure is not None else ""
        cover = bool(re.search(r"\bcover\b", n.get("alt", ""), re.I))
        if in_scope is None or cover or re.match(r"^fig(?:ure)?\.?\s*\d", legend, re.I):
            if cover and len(result.candidates) < 30:
                result.candidates.append(
                    Candidate(reference, "cover_image", "Cover imagery")
                )
            elif figure is not None and len(result.candidates) < 30:
                result.candidates.append(
                    Candidate(
                        reference,
                        "ordinary_figure",
                        "A figure outside a publisher graphical abstract role",
                    )
                )
            continue
        if https_host(reference) not in IMAGE_HOSTS[adapter]:
            result.candidates.append(
                Candidate(
                    reference,
                    "unverified_media_host",
                    "The graphical region links to an unapproved media host",
                )
            )
            result.slots.append(
                Slot(
                    f"ga_html_{i:03d}",
                    "graphical_abstract",
                    "web_only",
                    (),
                    legend,
                    evidence=f"{adapter}_html_graphical_section",
                    locator=doc.getroottree().getpath(n),
                    blocked_code="unverified_media_host",
                    blocked_reason="The graphic points outside the approved publisher media hosts",
                )
            )
            continue
        result.slots.append(
            Slot(
                f"ga_html_{i:03d}",
                "graphical_abstract",
                "web_only",
                (Variant(reference),),
                legend,
                evidence=f"{adapter}_html_graphical_section",
                locator=doc.getroottree().getpath(n),
            )
        )
    if (
        scopes
        and not result.slots
        and not any(
            c.reason_code in {"ordinary_figure", "cover_image"}
            for c in result.candidates
        )
    ):
        result.slots.append(
            Slot(
                "ga_html_empty",
                "graphical_abstract",
                "web_only",
                (),
                evidence=f"{adapter}_html_graphical_section",
                locator=doc.getroottree().getpath(scopes[0]),
            )
        )
    return result


def figure_repository_url(value):
    host = https_host(value)
    if host in {
        "api.figshare.com",
        "ndownloader.figshare.com",
        "figshare.com",
        "figshareusercontent.com",
        "pfigshare-u-files.s3.eu-west-1.amazonaws.com",
        "pfigshare-u-files.s3.amazonaws.com",
    }:
        return True
    if host in {"s3-eu-west-1.amazonaws.com", "s3.amazonaws.com"}:
        return urlsplit(value).path.startswith("/pfigshare-u-files/")
    return bool(host and host.endswith(".figshareusercontent.com"))


def figshare(data, doi, article_id, *, authorized=False):
    result = Discovery(scanned=True)
    ref = f"https://api.figshare.com/v2/articles/{article_id}"
    if not isinstance(data, dict) or data.get("id") != article_id:
        raise LookupError("Figshare item ID disagrees with the requested item")
    refs = data.get("references") or []
    related = {normalize_doi(item) for item in refs if isinstance(item, str)}
    related.update(
        normalize_doi(item.get("identifier"))
        for item in data.get("related_identifiers", [])
        if isinstance(item, dict)
        and item.get("relation_type", "").casefold()
        in {
            "issupplementto",
            "ispartof",
            "isderivedfrom",
            "references",
            "isassociatedwith",
        }
    )
    if normalize_doi(doi) not in related or not re.match(
        r"^graphical\s+abstract(?:$|\s*[:–—-])", data.get("title", ""), re.I
    ):
        result.candidates.append(
            Candidate(
                ref,
                "unverified_repository_relation",
                "The Figshare record does not declare a graphical abstract for this article DOI",
            )
        )
        return result
    license = data.get("license") or {}
    link = license.get("url", "")
    approved = https_host(link) in {
        "creativecommons.org",
        "www.creativecommons.org",
    } and (urlsplit(link).path.startswith(("/licenses/", "/publicdomain/zero/")))
    if not approved and not authorized:
        result.slots.append(
            Slot(
                "figshare_restricted",
                "graphical_abstract",
                "unspecified",
                (),
                evidence="figshare_declared_graphical_abstract",
                locator=ref,
                blocked_code="licence_not_authorized",
                blocked_reason="The linked repository item does not supply a public licence or explicit authorization",
            )
        )
        result.reason = "Figshare metadata identifies a related graphical abstract without a verified public licence or authorization"
        return result
    for entry in data.get("files", []):
        media = entry.get("downloadUrl") or entry.get("download_url") or ""
        if figure_repository_url(media):
            result.slots.append(
                Slot(
                    f"figshare_file_{entry['id']}",
                    "graphical_abstract",
                    "unspecified",
                    (Variant(media),),
                    evidence="figshare_declared_graphical_abstract",
                    locator=ref,
                    license=license.get("name") or link or None,
                )
            )
    if not result.slots:
        result.slots.append(
            Slot(
                "figshare_no_media",
                "graphical_abstract",
                "unspecified",
                (),
                evidence="figshare_declared_graphical_abstract",
                locator=ref,
            )
        )
    return result
