#!/usr/bin/env python3
"""Run the graphical abstract benchmark against real public publication data."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import collect_figures as core
import ga_cli
import ga_engine as engine
import ga_gallery
from ga_pdf import pdf_fallback
import ga_sources as sources
import publisher_adapters as publishers
from vor_audit import normalize_doi


def json_write(file, value):
    file.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def paper(case, out):
    pmcid = case["pmcid"]
    folder = out / "papers" / pmcid
    doi = normalize_doi(case["doi"])
    try:
        with (
            core.session() as session,
            TemporaryDirectory(prefix="pfc-public-pdf-") as directory,
        ):
            prefix, meta, xml = core.select_version(session, pmcid)
            if normalize_doi(meta.get("doi")) != doi or core.is_yes(
                meta.get("is_manuscript")
            ):
                raise ValueError(
                    "The public package did not identify the expected published DOI"
                )
            discovery = sources.jats(xml, doi)
            source = engine.Source(
                doi,
                "PMC_AWS",
                core.pmc_https_url(meta.get("xml_url") or ""),
                meta.get("license_code"),
                "published_in_pmc",
                prefix,
            )
            fallback = None
            if case.get("fallback_pdf_page") and not discovery.slots:
                pdf_url = core.pmc_https_url(meta.get("pdf_url") or "")
                if pdf_url is None:
                    raise LookupError(
                        "The source lacks the public publisher PDF for the fallback benchmark"
                    )
                binary, _ = ga_cli.public_get(session, pdf_url, core.allowed_pmc_url)
                pdf = Path(directory) / "publication.pdf"
                pdf.write_bytes(binary)
                fallback = pdf_fallback(
                    pdf, pdf_url, page=case["fallback_pdf_page"], doi=doi
                )
            access = (
                core.is_yes(meta.get("is_pmc_openaccess"))
                and str(meta.get("license_code", "")).upper() != "TDM"
            )

            def get(variant):
                if not access:
                    raise engine.AcquisitionError(
                        "outside_open_access_subset",
                        "Public package does not authorize media archiving",
                    )
                return core.fetch_graphical(
                    session, variant.reference, core.metadata_urls(meta), None
                )

            result = engine.acquire(
                discovery,
                source,
                folder,
                get,
                pdf=fallback,
                absence_url=case["evidence"] if case.get("confirm_absence") else None,
            )
    except Exception as exc:
        code, reason = engine.failure(exc)
        result = engine.make_report(
            doi,
            [],
            [],
            [
                {
                    "source": "live",
                    "scanned": False,
                    "reason_code": code,
                    "reason": reason,
                }
            ],
        )
        meta = {}
        discovery = engine.Discovery()
    folders = {
        "metadata": {
            "doi": doi,
            "title": meta.get("title") or doi,
            "source_version": "published_in_pmc",
            "source": "PMC_AWS",
        },
        "graphical_abstract": result,
        "figures": [],
    }
    folder.mkdir(parents=True, exist_ok=True)
    json_write(folder / "manifest.json", folders)
    json_write(folder / "graphical_abstract.json", result)
    ga_gallery.render(folder / "gallery.html", [(folder, folders)])
    asset_mismatch = False
    for record in result["records"]:
        asset = record["asset"]
        if not asset:
            continue
        if (
            case.get("expected_reference")
            and core.fmt_image_name(asset["source_url"]) != case["expected_reference"]
        ):
            asset_mismatch = True
        if case.get("fallback_pdf_page") and (
            asset["acquisition_method"] != "pdf_crop"
            or asset.get("pdf_crop", {}).get("page") != case["fallback_pdf_page"]
        ):
            asset_mismatch = True
    return {
        **case,
        "initial_structured_detection": bool(discovery.slots),
        "identified": bool(result["records"]),
        "status": result["status"],
        "counts": result["counts"],
        "reason": result["reason"],
        "candidates": result["candidates"],
        "sources": result["sources"],
        "acquisition_methods": dict(
            Counter(
                r["asset"]["acquisition_method"]
                for r in result["records"]
                if r["asset"]
            )
        ),
        "asset_mismatch": asset_mismatch,
    }


def probes(spec):
    outcomes = []
    for row in spec:
        try:
            with core.session() as session:
                response = publishers.safe_http_get(
                    session, row["url"], params=row["params"], max_size=1024 * 1024
                )
            result = {"http": 200, "bytes": len(response)}
        except publishers.PublisherHTTPError as exc:
            result = {"http": exc.status_code, "reason": str(exc)}
        except (OSError, ValueError) as exc:
            result = {"reason": publishers.publisher_error(exc)}
        outcomes.append({**row, **result})
    return outcomes


def cli():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "examples" / "ga_live_cases.json",
    )
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--probe-api",
        action="store_true",
        help="Measure unauthenticated official API access separately",
    )
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    spec = json.loads(args.cases.read_text("utf-8"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(
            pool.map(
                lambda c: paper(c, args.output), spec["cases"] + spec.get("review", [])
            )
        )
    labelled = [row for row in rows if "expected" in row]
    true_positive = sum(row["expected"] and row["identified"] for row in labelled)
    true_negative = sum(
        not row["expected"]
        and row["status"] in {"not_found_in_source", "confirmed_not_provided"}
        for row in labelled
    )
    unverified_negative = sum(
        not row["expected"]
        and not row["identified"]
        and row["status"] not in {"not_found_in_source", "confirmed_not_provided"}
        for row in labelled
    )
    fp = sum(not row["expected"] and row["identified"] for row in labelled)
    fn = sum(row["expected"] and not row["identified"] for row in labelled)
    wrong = sum(row["asset_mismatch"] for row in labelled)
    detected = sum(row["counts"]["detected"] for row in labelled)
    downloaded = sum(row["counts"]["downloaded"] for row in labelled)
    failures = Counter()
    for row in rows:
        failures.update(row["counts"]["reasons"])
        if row["status"] != "downloaded" and not row["counts"]["reasons"]:
            failures[row["status"]] += 1
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recognition": {
            "correct": true_positive + true_negative - wrong,
            "sample_articles": len(labelled),
            "true_positive": true_positive,
            "true_negative": true_negative,
            "false_positive": fp,
            "false_negative": fn,
            "unverified_negative": unverified_negative,
            "wrong_asset": wrong,
            "accuracy": (true_positive + true_negative - wrong) / len(labelled)
            if labelled
            else None,
            "structured_only_positive": sum(
                row["expected"] and row["initial_structured_detection"]
                for row in labelled
            ),
        },
        "acquisition": {
            "detected_images": detected,
            "downloaded_images": downloaded,
            "success_rate": downloaded / detected if detected else None,
            "methods": dict(
                sum((Counter(r["acquisition_methods"]) for r in labelled), Counter())
            ),
        },
        "misclassifications": {
            "count": fp + wrong,
            "sample_articles": len(labelled),
            "rate": (fp + wrong) / len(labelled) if labelled else None,
        },
        "not_acquired_reasons": dict(failures),
        "cases": rows,
        "api_probes": probes(spec.get("public_access_probes", []))
        if args.probe_api
        else [],
    }
    json_write(args.output / "metrics.json", report)
    ga_gallery.render(
        args.output / "index.html",
        [
            (
                args.output / "papers" / row["pmcid"],
                json.loads(
                    (args.output / "papers" / row["pmcid"] / "manifest.json").read_text(
                        "utf-8"
                    )
                ),
            )
            for row in rows
        ],
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "cases"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return (
        0
        if not (fp or fn or wrong or unverified_negative or detected != downloaded)
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(cli())
