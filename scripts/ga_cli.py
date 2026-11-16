#!/usr/bin/env python3
"""Archive a graphical abstract from authorized sources, or build a paper-first gallery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urljoin

from lxml import etree, html

import collect_figures as core
import ga_engine as engine
import ga_gallery
import ga_sources as sources
import publisher_adapters as publishers
from ga_pdf import pdf_fallback
from vor_audit import normalize_doi, read_index


def public_get(session, url, allowed, *, cap=engine.MAX_BYTES, figshare_token=None):
    """Only follow redirects to another address already in the supported public scope."""
    for _ in range(4):
        if not allowed(url):
            raise engine.AcquisitionError(
                "unverified_resource_url",
                "The resource is outside the configured official hosts",
            )
        headers = None
        if (
            figshare_token
            and sources.https_host(url) == "api.figshare.com"
            and url.startswith("https://api.figshare.com/v2/file/download/")
        ):
            headers = {"Authorization": "token " + figshare_token}
        with session.get(
            url, headers=headers, timeout=(10, 30), allow_redirects=False, stream=True
        ) as response:
            if response.status_code in (
                301,
                302,
                303,
                307,
                308,
            ) and response.headers.get("Location"):
                url = urljoin(url, response.headers["Location"])
                continue
            if response.status_code != 200:
                raise engine.AcquisitionError(
                    f"http_{response.status_code}",
                    f"HTTP {response.status_code} from the official resource",
                )
            data = bytearray()
            for block in response.iter_content(256 * 1024):
                if len(data) + len(block) > cap:
                    raise engine.AcquisitionError(
                        "response_too_large",
                        "The official resource exceeds the size limit",
                    )
                data.extend(block)
            return bytes(data), url
    raise engine.AcquisitionError(
        "too_many_redirects", "The official resource redirected more than three times"
    )


def open_license(page):
    root = html.fromstring(page, parser=html.HTMLParser(no_network=True))
    links = root.xpath('//link[@rel="license"]/@href|//a[@rel="license"]/@href')
    for link in links:
        if sources.https_host(link) in {
            "creativecommons.org",
            "www.creativecommons.org",
        } and ("/licenses/" in link or "/publicdomain/zero/" in link):
            return link
    return None


def blocked(code, detail):
    def fetch(_):
        raise engine.AcquisitionError(code, detail)

    return fetch


def acquire(args):
    doi = normalize_doi(args.doi)
    if not doi:
        raise ValueError("--doi must be a valid DOI or DOI URL")
    session = core.session()
    folder = args.output
    index = read_index(args.vor_index) if args.vor_index else None
    absence = core.absent_graphical_url(index, doi)
    license_code = args.license
    reports = []
    used = None

    def save(discovery, source, fetch, *, pdf=None):
        nonlocal used
        result = engine.acquire(
            discovery, source, folder, fetch, pdf=pdf, absence_url=absence
        )
        reports.append(result)
        if result["counts"]["downloaded"]:
            used = source

    def found():
        return any(r["counts"]["downloaded"] for r in reports)

    def failure(source, exc):
        code, reason = engine.failure(exc)
        reports.append(
            engine.make_report(
                doi,
                [],
                [],
                [
                    {
                        "source": source,
                        "scanned": False,
                        "reason_code": code,
                        "reason": reason,
                    }
                ],
            )
        )

    if args.local_xml:
        try:
            data = args.local_xml.read_bytes()
            parsed = sources.jats(data, doi)
            license_code = license_code or core.jats_article_metadata(
                core.jats_root(data)
            ).get("license_url")
            src = engine.Source(
                doi,
                "local_jats",
                args.local_xml.resolve().as_uri(),
                license_code,
                "authorized_local_jats",
            )
            save(
                parsed,
                src,
                lambda v: core.fetch_graphical(
                    session, v.reference, [], args.local_media
                ),
            )
        except (OSError, LookupError, ValueError, etree.XMLSyntaxError) as exc:
            failure("local_jats", exc)
    elif not args.skip_pmc:
        try:
            pmcid = core.resolve_pmcid(session, args.pmcid or doi)
            prefix, meta, data = core.select_version(session, pmcid)
            claimed = {
                normalize_doi(meta.get("doi")),
                sources.article_doi(core.jats_root(data)),
            } - {None}
            if claimed != {doi}:
                raise LookupError("PMC DOI does not match the requested article")
            detected = sources.jats(data, doi)
            license_code = license_code or meta.get("license_code")
            open_access = (
                core.is_yes(meta.get("is_pmc_openaccess"))
                and str(meta.get("license_code", "")).upper() != "TDM"
            )
            version = (
                "author_manuscript"
                if core.is_yes(meta.get("is_manuscript"))
                else "published_in_pmc"
            )
            source = engine.Source(
                doi,
                "PMC_AWS",
                core.pmc_https_url(meta.get("xml_url", "")),
                license_code,
                version,
                prefix,
            )
            save(
                detected,
                source,
                (
                    lambda v: core.fetch_graphical(
                        session, v.reference, core.metadata_urls(meta), None
                    )
                )
                if open_access
                else blocked(
                    "outside_open_access_subset",
                    "PMC record does not authorize image archive access",
                ),
            )
        except (OSError, LookupError, ValueError, etree.XMLSyntaxError) as exc:
            failure("PMC_AWS", exc)

    if not found() and args.publisher_api:
        route = publishers.provider_for(doi)
        if route in {"springer", "elsevier"}:
            publishers.collect_publisher(
                session, doi, folder, route, False, vor_index=index, graphical_only=True
            )
            reports.append(
                json.loads((folder / "graphical_abstract.json").read_text("utf-8"))
            )

    if args.article_url:
        try:
            host = sources.https_host(args.article_url)
            publisher = next(
                (
                    name
                    for name, hosts in sources.ARTICLE_HOSTS.items()
                    if host in hosts
                ),
                None,
            )
            if publisher is None:
                raise PermissionError(
                    "Only supported official publisher article URLs are accepted"
                )
            url = args.article_url
            if args.article_html:
                data = args.article_html.read_bytes()
            else:
                data, url = public_get(
                    session,
                    url,
                    lambda value: (
                        sources.https_host(value) in sources.ARTICLE_HOSTS[publisher]
                    ),
                    cap=15 * 1024 * 1024,
                )
            detected = sources.publisher_html(data, url, doi)
            page_license = open_license(data)
            src = engine.Source(
                doi,
                publisher + "_html",
                url,
                page_license or args.license,
                "publisher_web",
            )
            license_code = license_code or page_license
            callback = lambda v: public_get(
                session,
                v.reference,
                lambda u: sources.https_host(u) in sources.IMAGE_HOSTS[publisher],
            )
            save(
                detected,
                src,
                callback
                if page_license or args.authorized
                else blocked(
                    "licence_not_authorized",
                    "No open licence or explicit authorization accompanies this official article page",
                ),
            )
        except (OSError, LookupError, ValueError, etree.ParserError) as exc:
            failure("publisher_web", exc)

    if args.figshare_id:
        try:
            url = f"https://api.figshare.com/v2/articles/{args.figshare_id}"
            token = os.getenv("FIGSHARE_TOKEN", "").strip()
            if args.authorized and token:
                url = f"https://api.figshare.com/v2/account/articles/{args.figshare_id}"
                data = publishers.safe_http_get(
                    session,
                    url,
                    headers={"Authorization": "token " + token},
                    max_size=5 * 1024 * 1024,
                )
            else:
                data, _ = public_get(
                    session,
                    url,
                    lambda u: sources.https_host(u) == "api.figshare.com",
                    cap=5 * 1024 * 1024,
                )
            item = json.loads(data)
            detected = sources.figshare(
                item, doi, args.figshare_id, authorized=args.authorized
            )
            src = engine.Source(
                doi,
                "figshare",
                url,
                version="repository_item",
                version_id=str(item["version"])
                if item.get("version") is not None
                else None,
            )
            save(
                detected,
                src,
                lambda v: public_get(
                    session,
                    v.reference,
                    sources.figure_repository_url,
                    figshare_token=token if args.authorized else None,
                ),
            )
        except (OSError, LookupError, ValueError, etree.ParserError) as exc:
            failure("figshare", exc)

    print_records = [
        record
        for report in reports
        for record in report["records"]
        if record["edition"] == "print_only"
    ]
    needs_print = bool(print_records) and not any(
        record["status"] == "downloaded" for record in print_records
    )
    if args.pdf and not absence and (not found() or needs_print):
        src = engine.Source(
            doi, "publisher_pdf", args.pdf_url, license_code, "publisher_pdf"
        )
        save(
            engine.Discovery(),
            src,
            lambda _: (b"", ""),
            pdf=pdf_fallback(
                args.pdf, args.pdf_url, page=args.pdf_page, rect=args.pdf_rect, doi=doi
            ),
        )
    session.close()
    records = []
    for i, report in enumerate(reports):
        records.extend(
            {**record, "id": f"{i + 1}:{record['id']}"} for record in report["records"]
        )
    report = engine.make_report(
        doi,
        records,
        [c for entry in reports for c in entry["candidates"]],
        [row for entry in reports for row in entry["sources"]],
        absence_url=absence,
    )
    figures = []
    if report["records"]:
        graph = core.Figure(
            "graphical_abstract",
            "Graphical Abstract",
            "",
            "",
            "graphical_abstract",
            [r["reference"] for r in report["records"] if r["reference"]],
            label_source="xml_category",
        )
        core.attach_graphical_result(graph, report)
        figures.append(graph)
    version = (
        used.version
        if used
        else next(
            (x["asset"]["source_version"] for x in records if x.get("asset")), "unknown"
        )
    )
    metadata = {
        "doi": doi,
        "source": "Graphical_Abstract_Acquisition_Engine",
        "source_version": version,
        "license_code": license_code,
    }
    core.write_outputs(folder, figures, metadata, [], index, graphical=report)
    print(
        f"{doi} · {report['status']} · {report['counts']['unique_files']} files · {folder.resolve()}"
    )
    return 0 if report["status"] in {"downloaded", "confirmed_not_provided"} else 2


def gallery(args):
    run = args.run_dir.resolve()
    output = (args.output or run / "paper_gallery.html").resolve()
    if output.parent != run:
        raise ValueError(
            "The paper gallery must be in the archive root so relative media links work offline"
        )
    items = [
        (p.parent, json.loads(p.read_text("utf-8")))
        for p in sorted((run / "papers").glob("*/manifest.json"))
    ]
    if not items and (run / "manifest.json").is_file():
        items = [(run, json.loads((run / "manifest.json").read_text("utf-8")))]
    if not items:
        raise LookupError("No article manifests found in the archive")
    ga_gallery.render(output, items)
    print(f"{len(items)} paper cards · {output}")
    return 0


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(required=True, dest="command")
    p = modes.add_parser("acquire", help="Acquire and audit the graphical abstract")
    p.add_argument("--doi", required=True)
    p.add_argument("--pmcid")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--local-xml", type=Path)
    p.add_argument("--local-media", type=Path)
    p.add_argument("--skip-pmc", action="store_true")
    p.add_argument(
        "--publisher-api",
        action="store_true",
        help="Try an entitled publisher API when the PMC route has no download",
    )
    p.add_argument("--article-url", help="Exact official publisher article URL")
    p.add_argument(
        "--article-html",
        type=Path,
        help="Previously obtained HTML for the official article URL",
    )
    p.add_argument("--figshare-id", type=int)
    p.add_argument(
        "--authorized",
        action="store_true",
        help="Declare supplied publisher or repository access authorization",
    )
    p.add_argument(
        "--license", help="Known license for the provided authorized sources"
    )
    p.add_argument("--vor-index", type=Path)
    p.add_argument(
        "--pdf", type=Path, help="Authorized publication PDF used after original routes"
    )
    p.add_argument("--pdf-url", help="Source URL of the authorized publication PDF")
    p.add_argument("--pdf-page", type=int)
    p.add_argument("--pdf-rect", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
    view = modes.add_parser(
        "gallery", help="Build a paper-first gallery from article manifests"
    )
    view.add_argument("--run-dir", required=True, type=Path)
    view.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "acquire":
        if bool(args.local_xml) != bool(args.local_media):
            p.error("--local-xml and --local-media must be supplied together")
        if args.article_html and not args.article_url:
            p.error("--article-html requires --article-url")
        if (
            args.pdf
            and not args.pdf_url
            or not args.pdf
            and (args.pdf_url or args.pdf_page or args.pdf_rect)
        ):
            p.error("PDF fallback requires --pdf and --pdf-url")
        if args.pdf_rect and args.pdf_page is None:
            p.error("--pdf-rect requires --pdf-page")
        if args.figshare_id is not None and args.figshare_id <= 0:
            p.error("--figshare-id must be positive")
    try:
        return acquire(args) if args.command == "acquire" else gallery(args)
    except (OSError, ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
