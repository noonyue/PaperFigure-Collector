import hashlib
import json
import sys
from io import BytesIO
from pathlib import Path

import pytest
import requests
from lxml import html
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import collect_figures as core
import ga_engine as engine
import ga_sources as sources
import publisher_adapters as publishers
from ga_pdf import pdf_fallback

DOI = "10.1016/j.xcrm.2024.101715"
JATS = """<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta><article-id pub-id-type="doi">{doi}</article-id>{front}</article-meta></front>
  <body>{body}</body></article>"""
FIG1 = '<fig id="f1"><label>Figure 1</label><caption><p>Ordinary results</p></caption><graphic href="figure1.png"/></fig>'


def image_bytes(color="navy", kind="PNG", size=(80, 60)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=kind)
    return output.getvalue()


def document(front="", body=FIG1, doi=DOI):
    return JATS.format(front=front, body=body, doi=doi).encode()


def context(doi=DOI, kind="jats"):
    return engine.Source(
        doi, kind, f"https://doi.org/{doi}", "CC BY", "published_in_pmc"
    )


def test_jats_graphical_abstract_clips_caption_scope():
    doc = document("""<abstract abstract-type="graphical"><fig-group id="group"><fig id="a">
    <graphic href="panel-a.png"/><caption><p>Caption A <inline-graphic href="math.png"/></p></caption>
    </fig><fig id="b"><media href="panel-b.png" mimetype="image"/><caption><p>Caption B</p></caption></fig>
    </fig-group></abstract>""")
    discovery = sources.jats(doc, DOI)
    assert [slot.variants[0].reference for slot in discovery.slots] == [
        "panel-a.png",
        "panel-b.png",
    ]
    assert [slot.caption for slot in discovery.slots] == ["Caption A", "Caption B"]
    assert "figure1.png" not in [slot.variants[0].reference for slot in discovery.slots]


@pytest.mark.parametrize(
    "markup",
    [
        '<abstract class="graphical"><p><inline-graphic href="ga.png"/></p></abstract>',
        '<abstract><title>Graphical Abstract</title><graphic href="ga.png"/></abstract>',
        '<fig fig-type="graphical-abstract"><graphic href="ga.png"/></fig>',
    ],
)
def test_jats_graphical_role_markers(markup):
    finding = sources.jats(document(front=markup), DOI)
    assert [s.variants[0].reference for s in finding.slots] == ["ga.png"]


def test_versions_in_alternatives_never_cross_fallback(tmp_path):
    markup = """<abstract abstract-type="graphical"><alternatives>
    <graphic href="web.webp" specific-use="web-only"/>
    <graphic href="web.png" specific-use="web-only"/>
    <graphic href="print.tif" specific-use="print-only"/>
    </alternatives></abstract>"""
    discovery = sources.jats(document(markup), DOI)
    assert {slot.edition for slot in discovery.slots} == {"web_only", "print_only"}
    fetched = []
    data = image_bytes()

    def fetch(variant):
        fetched.append(variant.reference)
        if variant.reference in {"web.webp", "print.tif"}:
            raise engine.AcquisitionError("http_403", "HTTP 403")
        return data, "https://example.org/ga/web.png"

    result = engine.acquire(discovery, context(), tmp_path, fetch)
    rows = {row["edition"]: row for row in result["records"]}
    assert result["status"] == "downloaded"
    assert rows["web_only"]["asset"]["fallback_variant"]
    assert rows["web_only"]["asset"]["file"].endswith(".png")
    assert rows["print_only"]["status"] == "detected_not_accessible"
    assert rows["print_only"]["attempts"][0]["reason_code"] == "http_403"
    assert fetched.count("web.png") == 1


def test_multiple_slots_dedupe_bytes_but_preserve_captions_and_editions(tmp_path):
    discovery = sources.jats(
        document("""<abstract abstract-type="graphical">
    <fig><graphic href="one.jpg" specific-use="web-only"/><caption>One</caption></fig>
    <fig><graphic href="two.jpg" specific-use="web-only"/><caption>Two</caption></fig>
    <fig><graphic href="print.jpg" specific-use="print-only"/><caption>Print</caption></fig>
    </abstract>"""),
        DOI,
    )
    original = image_bytes(kind="JPEG")
    result = engine.acquire(
        discovery,
        context(),
        tmp_path,
        lambda variant: (original, "https://example.org/" + variant.reference),
    )
    items = result["records"]
    assert [item["caption"] for item in items] == ["One", "Two", "Print"]
    assert items[0]["asset"]["file"] == items[1]["asset"]["file"]
    assert items[2]["asset"]["file"] != items[0]["asset"]["file"]
    assert result["counts"]["unique_files"] == 2
    for row in items:
        asset = row["asset"]
        assert asset["sha256"] == hashlib.sha256(original).hexdigest()
        assert (asset["width"], asset["height"], asset["format"]) == (80, 60, "JPEG")
        assert asset["doi"] == DOI and asset["license"] == "CC BY"
        assert asset["source_version"] == "published_in_pmc"
        assert (tmp_path / asset["file"]).read_bytes() == original


def test_no_abstract_does_not_reuse_figure1(tmp_path):
    discovery = sources.jats(document(), DOI)

    def unexpected(_):
        pytest.fail("A main figure must never be fetched as an abstract")

    result = engine.acquire(discovery, context(), tmp_path, unexpected)
    assert result["status"] == "not_found_in_source"
    assert result["records"] == []
    result = engine.acquire(
        discovery,
        context(),
        tmp_path,
        unexpected,
        absence_url="https://example.org/complete-publisher-index",
    )
    assert result["status"] == "confirmed_not_provided"


def test_explicit_region_with_no_media_or_permission_is_reported(tmp_path):
    empty = sources.jats(
        document(
            '<abstract abstract-type="graphical"><title>Graphical Abstract</title></abstract>'
        ),
        DOI,
    )
    result = engine.acquire(
        empty, context(), tmp_path, lambda _: pytest.fail("No reference")
    )
    assert result["status"] == "detected_not_accessible"
    assert result["records"][0]["reason_code"] == "missing_reference"
    denied = sources.jats(
        document(
            '<abstract abstract-type="graphical"><graphic href="g.png"/></abstract>'
        ),
        DOI,
    )

    def fetch(_):
        raise requests.HTTPError(response=type("Response", (), {"status_code": 403})())

    result = engine.acquire(denied, context(), tmp_path, fetch)
    assert result["records"][0]["reason_code"] == "http_403"
    assert not (tmp_path / "graphical_abstract").exists()


def test_elsevier_locator_relationship_and_caption_conflict():
    xml = b"""<full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd">
    <coredata><doi>10.1016/j.xcrm.2024.101715</doi></coredata><originalText><doc>
      <ce:abstract class="graphical"><ce:figure id="s"><ce:caption>Correct legend</ce:caption>
          <ce:link locator="fx1"/></ce:figure></ce:abstract>
      <ce:figure id="main"><ce:label>Figure 1</ce:label>
        <ce:caption>Wrong legend</ce:caption><ce:link locator="gr1"/></ce:figure>
    </doc></originalText></full-text-retrieval-response>"""
    objects = [
        publishers.ElsevierRendition(
            "fx1",
            "https://api.elsevier.com/content/object/fx1",
            "IMAGE-HIGH-RES",
            80,
            60,
        ),
        publishers.ElsevierRendition(
            "gr1",
            "https://api.elsevier.com/content/object/gr1",
            "IMAGE-HIGH-RES",
            80,
            60,
        ),
    ]
    discovery = sources.elsevier(xml, objects, DOI)
    assert len(discovery.slots) == 1
    assert discovery.slots[0].caption == "Correct legend"
    assert discovery.slots[0].variants[0].reference.endswith("/fx1")
    bad = sources.elsevier(
        xml.replace(b'locator="gr1"', b'locator="fx1"'), objects, DOI
    )
    assert not bad.slots and bad.candidates[0].reason_code == "ambiguous_locator"


def test_springer_structural_marker_without_filename_guess():
    doi = "10.1038/s12276-026-01857-2"
    markup = '<abstract><p>Article summary</p><p><fig id="Figa" position="anchor"><graphic href="asset.webp"/></fig></p></abstract>'
    discovery = sources.jats(document(front=markup, doi=doi), doi)
    assert [s.variants[0].reference for s in discovery.slots] == ["asset.webp"]
    different = document(
        front='<abstract><p><graphic href="Figa_HTML.jpg"/></p></abstract>', doi=doi
    )
    assert not sources.jats(different, doi).slots
    assert sources.jats(different, doi).candidates


def test_taylor_francis_structured_html_and_social_candidate(tmp_path):
    doi = "10.1080/17568919.2024.2393569"
    page = """<html><head><meta name="citation_doi" content="{doi}">
      <meta property="og:image" content="https://www.tandfonline.com/social.jpg"></head><body>
      <div class="abstract graphical"><figure><img src="/ga.jpg"><figcaption>GA legend</figcaption></figure></div>
      <figure><img src="/ordinary.jpg"><figcaption>Figure 1</figcaption></figure></body></html>""".format(
        doi=doi
    ).encode()
    url = "https://www.tandfonline.com/doi/full/" + doi
    discovery = sources.publisher_html(page, url, doi)
    assert [slot.variants[0].reference for slot in discovery.slots] == [
        "https://www.tandfonline.com/ga.jpg"
    ]
    assert discovery.slots[0].caption == "GA legend"
    assert {item.reason_code for item in discovery.candidates} >= {
        "social_image",
        "ordinary_figure",
    }
    missing = page.replace(b"abstract graphical", b"abstract main")
    result = engine.acquire(
        sources.publisher_html(missing, url, doi),
        context(doi),
        tmp_path,
        lambda _: pytest.fail("Unconfirmed HTML image"),
    )
    assert result["status"] == "candidate_needs_review"
    with pytest.raises(LookupError, match="DOI"):
        sources.publisher_html(page, url, "10.1080/000000")


def test_figshare_requires_article_relation_explicit_role_and_license():
    doi = "10.1080/17568919.2024.2393569"
    related = {
        "id": 123,
        "title": "Graphical abstract",
        "references": [f"https://doi.org/{doi}"],
        "license": {
            "name": "CC BY 4.0",
            "url": "https://creativecommons.org/licenses/by/4.0/",
        },
        "files": [
            {
                "id": 7,
                "name": "arbitrary.png",
                "downloadUrl": "https://api.figshare.com/v2/file/download/7",
            }
        ],
    }
    entry = sources.figshare(related, doi, 123)
    assert len(entry.slots) == 1
    assert (
        entry.slots[0].variants[0].reference
        == "https://api.figshare.com/v2/file/download/7"
    )
    assert not sources.figshare({**related, "references": []}, doi, 123).slots
    assert (
        sources.figshare({**related, "license": {}}, doi, 123).slots[0].blocked_code
        == "licence_not_authorized"
    )
    assert not sources.figshare(
        {**related, "title": "Supplementary visual data"}, doi, 123
    ).slots


def test_labeled_pdf_is_last_resort(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    source_file = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((60, 60), "Graphical Abstract")
        page.insert_image(fitz.Rect(60, 80, 220, 200), stream=image_bytes())
        page.insert_text((60, 240), "Figure 1")
        page.insert_image(fitz.Rect(60, 260, 220, 380), stream=image_bytes("red"))
        doc.save(source_file)
    fallback = pdf_fallback(source_file, "https://example.org/published.pdf", page=1)
    with pytest.raises(ValueError, match="Graphical Abstract"):
        pdf_fallback(
            source_file,
            "https://example.org/published.pdf",
            page=1,
            rect=(60, 265, 220, 380),
        )()
    result = engine.acquire(
        engine.Discovery(),
        context(),
        tmp_path,
        lambda _: pytest.fail("No link"),
        pdf=fallback,
    )
    asset = result["records"][0]["asset"]
    assert asset["acquisition_method"] == "pdf_crop"
    assert asset["edition"] == "print_only"
    assert asset["source_url"] == "https://example.org/published.pdf"
    discovery = sources.jats(
        document(
            '<abstract abstract-type="graphical"><graphic href="ok.png"/></abstract>'
        ),
        DOI,
    )
    result = engine.acquire(
        discovery,
        context(),
        tmp_path,
        lambda _: (image_bytes(), "https://example.org/ga.png"),
        pdf=lambda: pytest.fail("PDF must be the last fallback"),
    )
    assert result["records"][0]["asset"]["acquisition_method"] == "original"


def test_default_jats_collection_and_offline_paper_sections(tmp_path):
    from urllib.parse import unquote

    body = f'{FIG1}<fig id="ed"><label>Extended Data Fig. 1</label><graphic href="ed.png"/></fig>'
    jats_path = tmp_path / "input.xml"
    jats_path.write_bytes(
        document(
            '<abstract abstract-type="graphical"><graphic href="g.png"/></abstract>',
            body,
        )
    )
    for name in ("g.png", "figure1.png", "ed.png"):
        (tmp_path / name).write_bytes(image_bytes())
    folder, stats = core.single_article(
        core.session(), None, tmp_path / "out", False, jats_path, tmp_path
    )
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert stats["downloaded_media_files"] == 3
    assert manifest["graphical_abstract"]["status"] == "downloaded"
    assert (folder / "graphical_abstract.json").is_file()
    gallery = html.fromstring((folder / "gallery.html").read_bytes())
    assert gallery.xpath('//article[@class="paper-card"]/section/@data-category') == [
        "graphical_abstract",
        "main",
        "extended_data",
    ]
    assert gallery.xpath("//script[@src]") == []
    for image in gallery.xpath("//img/@src"):
        assert (folder / unquote(image)).is_file()


def test_cli_scoped_local_xml_and_gallery(tmp_path):
    import ga_cli

    media = tmp_path / "media"
    media.mkdir()
    (media / "actual.png").write_bytes(image_bytes())
    local = tmp_path / "paper.xml"
    local.write_bytes(
        document(
            '<abstract abstract-type="graphical"><graphic href="actual.png"/></abstract>'
        )
    )
    folder = tmp_path / "output"
    assert (
        ga_cli.cli(
            [
                "acquire",
                "--doi",
                DOI,
                "--local-xml",
                str(local),
                "--local-media",
                str(media),
                "--output",
                str(folder),
            ]
        )
        == 0
    )
    report = json.loads((folder / "graphical_abstract.json").read_text("utf-8"))
    assert report["records"][0]["asset"]["source_version"] == "authorized_local_jats"
    assert ga_cli.cli(["gallery", "--run-dir", str(folder)]) == 0
    assert (folder / "paper_gallery.html").is_file()


@pytest.mark.parametrize(
    "href", ["../secret.png", "%2e%2e/secret.png", "//untrusted.example/secret.png"]
)
def test_local_graphical_references_cannot_escape_media_dir(tmp_path, href):
    with pytest.raises(engine.AcquisitionError) as exc:
        core.fetch_graphical(core.session(), href, [], tmp_path)
    assert exc.value.code == "unsafe_reference"


def test_figshare_unauthorized_record_stays_detected_not_accessible(tmp_path):
    doi = "10.1080/17568919.2024.2393569"
    data = {
        "id": 123,
        "title": "Graphical Abstract",
        "references": [f"https://doi.org/{doi}"],
    }
    discovery = sources.figshare(data, doi, 123)
    result = engine.acquire(
        discovery, context(doi), tmp_path, lambda _: pytest.fail("Unlicensed media")
    )
    assert result["status"] == "detected_not_accessible"
    assert result["counts"]["reasons"] == {"licence_not_authorized": 1}


def test_ordinary_meta_locator_with_graphical_looking_id_is_not_authority():
    items = [
        publishers.ElsevierRendition(
            "ga1",
            "https://api.elsevier.com/content/object/ga1",
            "IMAGE-HIGH-RES",
            80,
            60,
        )
    ]
    discovery = sources.elsevier(None, items, DOI)
    assert not discovery.slots
    assert discovery.candidates[0].reason_code == "unverified_object_role"
    assert publishers.group_elsevier_renditions(items)[0].category == "unclassified"


def test_meta_declared_version_or_ce_variant_version_is_distinct(tmp_path):
    xml = b"""<doc xmlns:ce="http://www.elsevier.com/xml/common/dtd">
    <ce:abstract class="graphical"><ce:link locator="asset" href="unrelated-display-preview.jpg"/></ce:abstract></doc>"""
    items = [
        publishers.ElsevierRendition(
            "asset",
            "https://api.elsevier.com/content/object/web",
            "IMAGE-HIGH-RES",
            80,
            60,
            "web_only",
        ),
        publishers.ElsevierRendition(
            "asset",
            "https://api.elsevier.com/content/object/print",
            "IMAGE-HIGH-RES",
            80,
            60,
            "print_only",
        ),
    ]
    detection = sources.elsevier(xml, items, DOI)
    assert [slot.edition for slot in detection.slots] == ["web_only", "print_only"]
    report = engine.acquire(
        detection, context(), tmp_path, lambda v: (image_bytes(), v.reference)
    )
    assert report["counts"]["unique_files"] == 2


def test_bad_binary_not_archived_and_presigned_tokens_are_sanitized(tmp_path):
    discovery = sources.jats(
        document(
            '<abstract abstract-type="graphical"><graphic href="ga.png"/></abstract>'
        ),
        DOI,
    )

    def fetch(_):
        return (
            b"<html>Access page</html>",
            "https://media.springernature.com/x?token=private",
        )

    result = engine.acquire(discovery, context(), tmp_path, fetch)
    assert result["status"] == "detected_not_accessible"
    assert result["counts"]["reasons"] == {"invalid_media": 1}
    assert "private" not in json.dumps(result)
    assert engine.clean_reference(
        "https://user:private@media.springernature.com/x?token=private"
    ) == ("https://media.springernature.com/x")


def test_svg_dimensions_keep_source_units():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120.5 46"><rect width="4" height="6"/></svg>'
    assert engine.dimensions(svg) == ("SVG", 120.5, 46, "svg_viewbox_units")


def test_ambiguous_identical_reference_in_pmc_media_inventory_is_blocked(tmp_path):
    urls = [
        "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/a/ga.png",
        "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/b/ga.png",
    ]
    with pytest.raises(engine.AcquisitionError) as e:
        core.fetch_graphical(core.session(), "ga.png", urls, None)
    assert e.value.code == "ambiguous_reference"


def test_unknown_source_reports_unknown_instead_of_absence(tmp_path):
    result = engine.acquire(
        engine.Discovery(reason="Access to primary source failed"),
        context(),
        tmp_path,
        lambda _: pytest.fail("No source"),
    )
    assert result["status"] == "unknown"
    assert result["sources"][0]["reason"] == "Access to primary source failed"


def test_published_pdf_parallel_column_does_not_cut_off_the_abstract(tmp_path):
    fitz = pytest.importorskip("pymupdf")
    pdf = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((220, 55), "Graphical Abstract")
        page.insert_text((20, 90), "Introduction")
        page.insert_image(fitz.Rect(280, 85, 475, 230), stream=image_bytes())
        doc.save(pdf)
    data, _, info = pdf_fallback(pdf, "https://example.org/authorized.pdf", page=1)()
    assert info["rect_pt"][0] >= 280
    assert engine.dimensions(data)[0] == "PNG"


def test_scoped_web_client_redirects_never_forward_figshare_token():
    from ga_cli import public_get

    class Response:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def iter_content(self, _):
            yield b"public file"

    class Session:
        def __init__(self, location):
            self.location = location
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs["headers"]))
            return (
                Response(302, {"Location": self.location})
                if len(self.calls) == 1
                else Response(200)
            )

    source = "https://api.figshare.com/v2/file/download/7"
    destination = "https://ndownloader.figshare.com/files/7"
    session = Session(destination)
    assert public_get(
        session, source, sources.figure_repository_url, figshare_token="secret"
    ) == (b"public file", destination)
    assert session.calls == [
        (source, {"Authorization": "token secret"}),
        (destination, None),
    ]
    session = Session("https://external.example/unknown")
    with pytest.raises(engine.AcquisitionError) as error:
        public_get(session, source, sources.figure_repository_url)
    assert error.value.code == "unverified_resource_url"
    assert len(session.calls) == 1

    with pytest.raises(engine.AcquisitionError) as error:
        public_get(Session(destination), source, sources.figure_repository_url, cap=4)
    assert error.value.code == "response_too_large"


def test_explicit_web_edition_and_primary_print_edition_keep_both(
    tmp_path, monkeypatch
):
    from ga_cli import cli

    doi = "10.1080/10717544.2024.2361168"
    local = tmp_path / "paper.xml"
    local.write_bytes(
        document(
            '<abstract abstract-type="graphical"><graphic specific-use="print-only" href="g.png"/></abstract>',
            doi=doi,
        )
    )
    (tmp_path / "g.png").write_bytes(image_bytes())
    page = tmp_path / "page.html"
    page.write_text(f'''<html><head><meta name="citation_doi" content="{doi}">
      <link rel="license" href="https://creativecommons.org/licenses/by/4.0/"></head>
      <body><section class="abstract graphical"><img src="/web.png"></section></body></html>''')
    calls = []

    def fetch(_, url, allowed, **options):
        calls.append(url)
        assert allowed(url)
        return image_bytes(), url

    monkeypatch.setattr("ga_cli.public_get", fetch)
    archive = tmp_path / "archive"
    assert (
        cli(
            [
                "acquire",
                "--doi",
                doi,
                "--local-xml",
                str(local),
                "--local-media",
                str(tmp_path),
                "--article-url",
                f"https://www.tandfonline.com/doi/full/{doi}",
                "--article-html",
                str(page),
                "--output",
                str(archive),
            ]
        )
        == 0
    )
    report = json.loads((archive / "graphical_abstract.json").read_text("utf-8"))
    assert [row["edition"] for row in report["records"]] == ["print_only", "web_only"]
    assert report["counts"]["unique_files"] == 2
    assert calls == ["https://www.tandfonline.com/web.png"]


def test_pmc_non_oa_labeled_image_records_access_failure(tmp_path, monkeypatch):
    from ga_cli import cli

    monkeypatch.setattr("ga_cli.core.resolve_pmcid", lambda *_: "PMC1")
    monkeypatch.setattr(
        "ga_cli.core.select_version",
        lambda *_: (
            "PMC1.1",
            {"doi": DOI, "is_pmc_openaccess": False, "license_code": "TDM"},
            document(
                '<abstract abstract-type="graphical"><graphic href="explicit.png"/></abstract>'
            ),
        ),
    )
    assert cli(["acquire", "--doi", DOI, "--output", str(tmp_path)]) == 2
    report = json.loads((tmp_path / "graphical_abstract.json").read_text("utf-8"))
    assert report["status"] == "detected_not_accessible"
    assert report["counts"]["reasons"] == {"outside_open_access_subset": 1}


def test_source_authority_cannot_bind_plain_object_to_publisher_figure():
    declared = b"""<attachment-metadata-response><attachment><ref>paired</ref>
    <url>https://api.elsevier.com/content/object/eid/real.png</url>
    <mimetype>image/png</mimetype><type>IMAGE-HIGH-RES</type>
    <edition>web-only</edition><object-role>graphical-abstract</object-role>
    </attachment></attachment-metadata-response>"""
    assets = publishers.elsevier_renditions(declared, DOI)
    assert assets[0].edition == "web_only" and assets[0].graphical_role
    evidence = sources.elsevier(None, assets, DOI)
    assert (
        len(evidence.slots) == 1
        and evidence.slots[0].evidence == "elsevier_meta_explicit_role"
    )


def test_competing_jats_reference_does_not_borrow_main_image():
    doc = document(
        '<abstract abstract-type="graphical"><graphic href="figure1.png"/></abstract>'
    )
    detection = sources.jats(doc, DOI)
    assert not detection.slots
    assert detection.candidates[0].reason_code == "ambiguous_locator"
    assert [figure.category for figure in core.parse_jats(doc)] == ["main"]
