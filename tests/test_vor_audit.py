import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_figures as core
import vor_audit as audit

DOI = "10.1234/batch-figures"
JATS = b"""<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta>
    <article-id pub-id-type="doi">10.1234/BATCH-FIGURES</article-id>
    <permissions><license xlink:href="https://creativecommons.org/licenses/by/4.0/"/></permissions>
    <abstract abstract-type="graphical"><fig id="undfig1"><graphic xlink:href="ga.png"/></fig></abstract>
  </article-meta></front>
  <body><fig id="f1"><label>Fig. 1.</label>
    <caption><p>See Supplementary Figure S1 and Extended Data Fig. 1.</p></caption>
    <graphic xlink:href="main.png"/>
  </fig></body>
  <back><app-group>
    <app><sec><p><fig id="ed1"><label>Extended Data Fig. 1</label>
      <graphic xlink:href="ed.png"/>
    </fig></p></sec></app>
    <app><fig id="s1"><label>Supplementary Figure S1</label><graphic xlink:href="supp.png"/></fig></app>
  </app-group></back>
</article>"""


def write_reference(tmp_path, figures, categories=("main", "graphical_abstract", "extended_data")):
    path = tmp_path / "reference.json"
    path.write_text(json.dumps({"articles": [{
        "doi": DOI.upper(),
        "source": {"kind": "publisher_figure_index", "url": "https://example.org/record",
                   "checked_on": "2026-11-16"},
        "categories": list(categories),
        "figures": [{"category": category, "label": label} for category, label in figures],
    }]}), encoding="utf-8")
    return path


def local_archive(tmp_path, figures):
    xml = tmp_path / "article.xml"
    xml.write_bytes(JATS)
    media = tmp_path / "media"
    media.mkdir()
    for name in ("main", "ga", "ed"):
        Image.new("RGB", (32, 20)).save(media / f"{name}.png")
    return xml, media, write_reference(tmp_path, figures)


def test_cli_audits_real_jats_layout_and_existing_archive(tmp_path):
    xml, media, index = local_archive(tmp_path, [
        ("graphical_abstract", "Graphical Abstract"), ("main", "Figure 01"),
        ("extended_data", "Extended Data Figure 1"),
    ])
    folder = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts/publisher_adapters.py"),
         "--local-xml", str(xml), "--local-media", str(media), "--output", str(folder),
         "--vor-index", str(index)],
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    path = folder / xml.stem / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    report = manifest["vor_audit"]
    assert manifest["metadata"]["doi"] == DOI
    assert manifest["metadata"]["license_code"] == "CC BY"
    assert report["status"] == "matched"
    assert report["counts"]["matched_labels"] == 3
    assert report["counts"]["verified_figures"] == 3
    assert report["counts"]["captioned_figures"] == 1
    assert report["version_status"] == "not_verified"
    assert [f["category"] for f in manifest["figures"]] == ["graphical_abstract", "main", "extended_data"]
    asset = path.parent / manifest["figures"][1]["images"][0]["file"]
    asset.write_bytes(b"corrupted after archive")
    proc = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts/vor_audit.py"),
         "--manifest", str(path), "--vor-index", str(index)],
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 2
    report = json.loads(proc.stdout)
    assert report["status"] == "incomplete"
    assert report["figures"][1]["assets"][0]["status"] == "hash_mismatch"


def test_missing_records_and_local_assets_are_independent(tmp_path):
    xml, media, index = local_archive(tmp_path, [("main", "Fig. 1"), ("main", "Fig. 2")])
    (media / "main.png").unlink()
    path, stats = core.single_article(
        core.session(), None, tmp_path / "out", False, xml, media, vor_index=audit.read_index(index)
    )
    document = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert stats["vor_status"] == "incomplete"
    assert [row["status"] for row in document["vor_audit"]["figures"]] == ["asset_unavailable", "missing_record"]


def test_duplicate_unverified_and_extra_labels(tmp_path):
    index = audit.read_index(write_reference(tmp_path, [("main", "Fig. 1"), ("main", "Fig. 2")]))
    asset = tmp_path / "img.png"
    asset.write_bytes(b"test content")
    image = {"status": "downloaded", "file": asset.name,
             "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
             "quality_tier": "IMAGE-THUMBNAIL_fallback"}
    figures = [
        {"id": i, "label": label, "label_source": origin, "category": "main",
         "status": "downloaded", "images": [image]}
        for i, (label, origin) in enumerate([
            ("Fig. 1", "xml"), ("Figure 1.", "xml"),
            ("Fig. 2", None), ("Fig. 3", "xml"), ("Fig. 4", "object_reference"),
        ])
    ]
    manifest = {"metadata": {"doi": DOI}, "figures": figures, "errors": []}
    report = audit.compare(manifest, index, tmp_path)
    assert report["status"] == "needs_review"
    assert [r["status"] for r in report["figures"]] == ["ambiguous", "unverified_label"]
    assert report["counts"]["lower_resolution_figures"] == 1
    assert report["counts"]["unexpected_records"] == 1
    assert report["counts"]["unresolved_records"] == 1


def test_author_manuscript_never_promotes_matched_labels_to_matched_status(tmp_path):
    index = audit.read_index(write_reference(tmp_path, [("main", "Fig. 1")]))
    image = tmp_path / "img"
    image.write_bytes(b"file")
    manifest = {
        "metadata": {"doi": DOI, "source_version": "author_manuscript"},
        "figures": [{
            "id": "f1", "label": "Fig. 1", "label_source": "xml", "category": "main",
            "status": "downloaded", "images": [{
                "status": "downloaded", "file": "img", "sha256": hashlib.sha256(b"file").hexdigest(),
            }],
        }],
    }
    report = audit.compare(manifest, index, tmp_path)
    assert report["status"] == "needs_review"
    assert report["counts"]["verified_figures"] == 1
    assert report["version_status"] == "author_manuscript"
    with pytest.raises(ValueError, match="agree"):
        audit.compare(manifest, index, tmp_path, "10.1234/other")
    manifest["metadata"] = {"doi": "10.1234/other"}
    assert audit.compare(manifest, index, tmp_path)["reason"] == "no_reference_for_doi"


def test_reference_rejects_duplicate_canonical_labels(tmp_path):
    index = write_reference(tmp_path, [("main", "Fig. 1."), ("main", "Figure 01")])
    with pytest.raises(ValueError, match="duplicate figure label"):
        audit.read_index(index)


def test_additional_jats_graphics_and_scoped_pmc_s3_addresses():
    xml = b"""<article xmlns:xlink="http://www.w3.org/1999/xlink">
      <front><article-meta><abstract><title>Graphical abstract</title>
        <p><inline-graphic xlink:href="ga.png"/></p></abstract></article-meta></front>
      <body><fig-group id="g"><label>Fig. 1</label><fig><graphic href="a.png"/></fig>
        <fig><graphic href="b.png"/></fig></fig-group></body>
    </article>"""
    figures = core.parse_jats(xml)
    assert [(f.label_source, f.category, f.references) for f in figures] == [
        ("xml_category", "graphical_abstract", ["ga.png"]), ("xml", "main", ["a.png", "b.png"]),
    ]
    assert core.pmc_https_url("s3://pmc-oa-opendata/PMC1.1/f1.jpg?md5=abc") == (
        "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/f1.jpg?md5=abc"
    )
    assert core.pmc_https_url("s3://another-bucket/PMC1.1/f1.jpg") is None
    assert core.pmc_https_url("https://s3.amazonaws.com/pmc-oa-opendata/%2e%2e/other") is None
    assert core.pmc_https_url("https://pmc-oa-opendata.s3.amazonaws.com:bad/file") is None


def test_doi_resolution_falls_back_to_pmc_exact_esearch():
    from requests import HTTPError

    class ConverterUnavailable:
        def raise_for_status(self):
            raise HTTPError("Converter returned 403")

    class SearchResult:
        def __init__(self, ids):
            self.ids = ids

        def raise_for_status(self):
            pass

        def json(self):
            return {"esearchresult": {"count": str(len(self.ids)), "idlist": self.ids}}

    class Session:
        def __init__(self, ids):
            self.ids = ids

        def get(self, url, **kwargs):
            if url == core.ID_API:
                return ConverterUnavailable()
            assert url == core.PMC_SEARCH_API
            assert kwargs["params"]["term"] == '"10.1234/batch-figures"[lid]'
            assert kwargs["params"]["db"] == "pmc"
            return SearchResult(self.ids)

    assert core.resolve_pmcid(Session(["1234567"]), DOI) == "PMC1234567"
    with pytest.raises(LookupError, match="no unique article match"):
        core.resolve_pmcid(Session(["1234567", "2345678"]), DOI)

