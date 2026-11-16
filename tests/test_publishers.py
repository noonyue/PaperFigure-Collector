import importlib.util
import os
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))
import publisher_adapters as p

SPRINGER_RESPONSE = b'''<response><records><article xmlns:xlink="http://www.w3.org/1999/xlink">
<front><article-meta><article-id pub-id-type="doi">10.1038/s41586-026-00000-1</article-id></article-meta></front>
<body><fig id="f1"><label>Fig. 1</label><caption><p>First figure</p></caption>
<graphic xlink:href="https://media.springernature.com/fig1.png" /></fig>
<fig id="f2"><label>Fig. 2</label><graphic xlink:href="f2.jpg" /></fig></body></article>
</records></response>'''

ELSEVIER_META = b'''<attachment-metadata-response xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
<attachment><prism:url>https://api.elsevier.com/content/object/eid/paper-gr1.jpg</prism:url>
<ref>gr1</ref><filename>gr1.jpg</filename><mimetype>image/jpeg</mimetype><type>IMAGE-DOWNSAMPLED</type></attachment>
<attachment><prism:url>https://api.elsevier.com/content/object/eid/paper-gr1_lrg.jpg</prism:url>
<ref>gr1</ref><filename>gr1_lrg.jpg</filename><mimetype>image/jpeg</mimetype><type>IMAGE-HIGH-RES</type></attachment>
<attachment><prism:url>https://api.elsevier.com/content/object/eid/paper-gr1.sml</prism:url>
<ref>gr1</ref><filename>gr1.sml</filename><mimetype>image/gif</mimetype><type>IMAGE-THUMBNAIL</type></attachment>
<attachment><prism:url>https://bad.example/content/object/ga.png</prism:url>
<ref>ga1</ref><filename>ga.png</filename><mimetype>image/png</mimetype><type>IMAGE-HIGH-RES</type></attachment>
<attachment><prism:url>https://api.elsevier.com/content/object/eid/supp.pdf</prism:url>
<ref>sup1</ref><filename>supp.pdf</filename><mimetype>application/pdf</mimetype><type>STANDARD</type></attachment>
</attachment-metadata-response>'''

FULLTEXT = b'''<xocs:doc xmlns:xocs="http://www.elsevier.com/xml/xocs" xmlns:ce="http://www.elsevier.com/xml/common/dtd">
<ce:figure><ce:label>Fig. 1</ce:label><ce:caption><ce:simple-para>Caption from publisher full text</ce:simple-para></ce:caption>
<ce:link locator="gr1"/></ce:figure></xocs:doc>'''


def test_doi_route_and_url_safety():
    assert p.provider_for('10.1038/foo') == 'springer'
    assert p.provider_for('10.1016/j.cell.2026.01.001') == 'elsevier'
    assert p.provider_for('10.1126/science.foo') == 'science'
    assert not p.allowed_publisher_url('http://api.elsevier.com/content/object/x', 'elsevier')
    assert not p.allowed_publisher_url('https://api.elsevier.com.evil.test/content/object/x', 'elsevier')
    assert not p.allowed_publisher_url('https://evil.test/a.png', 'springer')
    assert not p.allowed_publisher_url('https://username:password@media.springernature.com/x.jpg', 'springer')


def test_springer_matching_doi_and_figures():
    xml = p.find_jats_article(SPRINGER_RESPONSE, '10.1038/s41586-026-00000-1')
    figures = p.core.parse_jats(xml)
    assert [f.label for f in figures] == ['Fig. 1', 'Fig. 2']
    assert figures[0].caption == 'First figure'
    assert p.allowed_publisher_url(figures[0].references[0], 'springer')
    assert not p.allowed_publisher_url(figures[1].references[0], 'springer')
    with pytest.raises(LookupError):
        p.find_jats_article(SPRINGER_RESPONSE, '10.1038/other')


def test_elsevier_group_hires_captions_and_segregate_unsafe():
    figures = p.parse_elsevier_attachments(ELSEVIER_META)
    assert len(figures) == 1
    assert figures[0].id == 'gr1'
    assert figures[0].references == ['https://api.elsevier.com/content/object/eid/paper-gr1_lrg.jpg']
    assert p.attach_elsevier_captions(figures, FULLTEXT) == 1
    assert figures[0].caption == 'Caption from publisher full text'
    assert p.elsevier_quality_map(ELSEVIER_META)[figures[0].references[0]] == 'IMAGE-HIGH-RES'


def test_no_api_key_has_audited_error(tmp_path, monkeypatch):
    monkeypatch.delenv('SPRINGER_NATURE_API_KEY', raising=False)
    stats = p.collect_publisher(p.core.session(), '10.1038/fake', tmp_path/'paper', 'springer', False)
    assert stats['complete'] is False
    assert (tmp_path/'paper'/'audit.md').exists()
    audit = (tmp_path/'paper'/'audit.md').read_text()
    assert 'SPRINGER_NATURE_API_KEY' in audit
    assert stats['downloaded_media_files'] == 0


def test_official_high_res_mocked_binary_download(tmp_path, monkeypatch):
    figures = p.parse_elsevier_attachments(ELSEVIER_META)
    def image_bytes(size):
        buf = BytesIO()
        Image.new('RGB', size).save(buf, format='JPEG')
        return buf.getvalue()
    seen = []
    def get_binary(session, url, **kwargs):
        seen.append(url)
        return image_bytes((2000, 1000))
    monkeypatch.setattr(p, 'safe_http_get', get_binary)
    monkeypatch.setattr(p.time, 'sleep', lambda x: None)
    p.save_publisher_figures(figures, tmp_path, p.core.session(), 'elsevier', {'X-ELS-APIKey': 'fake'},
                             quality_by_url=p.elsevier_quality_map(ELSEVIER_META))
    assert seen == [figures[0].references[0]]
    assert figures[0].status == 'downloaded'
    assert figures[0].images[0]['quality_tier'] == 'IMAGE-HIGH-RES'
    assert (tmp_path / figures[0].images[0]['file']).exists()


def test_safe_http_does_not_follow_redirects():
    class Response:
        status_code = 302
        def close(self): pass
    class Session:
        def get(self, *a, **kw):
            assert kw['allow_redirects'] is False
            return Response()
    with pytest.raises(PermissionError, match='302'):
        p.safe_http_get(Session(), 'https://api.elsevier.com/content/object/eid/example')


def test_springer_adapter_end_to_end_mock(tmp_path, monkeypatch):
    monkeypatch.setenv('SPRINGER_NATURE_API_KEY', 'LOCAL_FAKE_TOKEN')
    monkeypatch.setattr(p.time, 'sleep', lambda x: None)
    buffer = BytesIO()
    Image.new('RGB', (640, 480)).save(buffer, format='PNG')
    calls = []
    def fake_get(session, url, **kw):
        calls.append(url)
        if url == p.SPRINGER_API:
            assert kw['params']['q'] == 'doi:10.1038/s41586-026-00000-1'
            return SPRINGER_RESPONSE
        if url == 'https://media.springernature.com/fig1.png':
            assert kw.get('headers') is None
            return buffer.getvalue()
        raise AssertionError('Unexpected network call')
    monkeypatch.setattr(p, 'safe_http_get', fake_get)
    folder = tmp_path/'springer'
    stats = p.collect_publisher(p.core.session(), '10.1038/s41586-026-00000-1', folder, 'springer', False)
    assert stats['downloaded_media_files'] == 1
    assert stats['expected_figure_records'] == 2
    assert stats['missing_figure_records'] == 1  # no invented URL for relative image
    report = (folder/'manifest.json').read_text()
    assert 'LOCAL_FAKE_TOKEN' not in report
    assert 'f2.jpg' in report
    assert (folder/'main'/'001_f1_01.png').exists()


def test_elsevier_adapter_end_to_end_mock(tmp_path, monkeypatch):
    monkeypatch.setenv('ELSEVIER_API_KEY', 'LOCAL_SECRET_KEY')
    monkeypatch.setattr(p.time, 'sleep', lambda x: None)
    buffer = BytesIO()
    Image.new('RGB', (1200, 700)).save(buffer, format='JPEG')
    def fake_get(session, url, **kw):
        if '/object/doi/' in url:
            assert kw['headers']['X-ELS-APIKey'] == 'LOCAL_SECRET_KEY'
            return ELSEVIER_META
        if '/article/doi/' in url:
            return FULLTEXT
        if url.endswith('gr1_lrg.jpg'):
            return buffer.getvalue()
        raise AssertionError(f'Unexpected network call: {url}')
    monkeypatch.setattr(p, 'safe_http_get', fake_get)
    folder = tmp_path/'elsevier'
    stats = p.collect_publisher(p.core.session(), '10.1016/j.cell.2026.01.001', folder, 'elsevier', False)
    assert stats['downloaded_media_files'] == 1
    records = __import__('json').loads((folder/'manifest.json').read_text())
    assert records['figures'][0]['images'][0]['quality_tier'] == 'IMAGE-HIGH-RES'
    assert records['figures'][0]['caption'] == 'Caption from publisher full text'
    assert 'LOCAL_SECRET_KEY' not in (folder/'audit.md').read_text()
    assert 'LOCAL_SECRET_KEY' not in (folder/'manifest.json').read_text()


def test_elsevier_documented_payloads_and_resolution_fallback(tmp_path, monkeypatch):
    fixtures = SCRIPT_DIR.parent / "tests" / "fixtures"
    meta = (fixtures / "elsevier_meta.xml").read_bytes()
    full = (fixtures / "elsevier_full.xml").read_bytes()
    monkeypatch.setenv("ELSEVIER_API_KEY", "fixture_secret")
    monkeypatch.setattr(p.time, "sleep", lambda _: None)

    def image_response(size):
        buf = BytesIO()
        Image.new("RGB", size).save(buf, format="JPEG")
        return buf.getvalue()

    def get_response(session, url, **kwargs):
        if kwargs.get("params") == {"view": "META"}:
            return meta
        if kwargs.get("params") == {"view": "FULL"}:
            return full
        assert url.startswith("https://api.elsevier.com/content/object/")
        if "fixture-fx1.jpg" in url:
            return image_response((100, 70))
        assert "view=HIGH" in url or "view=STANDARD" in url
        # The high rendition delivers the standard dimensions. Only the standard
        # rendition can be accepted with this response.
        return image_response((30, 21))

    monkeypatch.setattr(p, "safe_http_get", get_response)
    stats = p.collect_publisher(p.core.session(), "10.1016/example", tmp_path, "elsevier", False)
    assert stats["expected_figure_records"] == 3
    assert stats["downloaded_media_files"] == 2
    assert stats["missing_figure_records"] == 1
    manifest = __import__("json").loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    f1, f2, abstract = manifest["figures"]
    assert (f1["label"], f1["label_source"], f1["caption"]) == ("Fig. 1", "xml", "Fixture main caption")
    assert f1["images"][0]["quality_tier"] == "IMAGE-DOWNSAMPLED_fallback"
    assert "dimensions differ" in f1["images"][0]["attempts"][0]["error"]
    assert f2["status"] == "missing" and f2["images"][0]["reference"] == "gr2"
    assert (abstract["id"], abstract["category"], abstract["label_source"]) == (
        "fx1", "graphical_abstract", "xml_category"
    )
    with pytest.raises(LookupError, match="META DOI"):
        p.elsevier_renditions(meta, "10.1016/different")
    with pytest.raises(LookupError, match="FULL DOI"):
        p.attach_elsevier_captions([], full, "10.1016/different")


def test_springer_provenance_redacts_credentials_in_media_urls(tmp_path, monkeypatch):
    from requests import ConnectionError

    monkeypatch.setenv("SPRINGER_NATURE_API_KEY", "fixture_secret")
    monkeypatch.setattr(p.time, "sleep", lambda _: None)
    xml = SPRINGER_RESPONSE.replace(b"/fig1.png", b"/fig1.png?api_key=fixture_secret")

    def fail_media(session, url, **kwargs):
        if url == p.SPRINGER_API:
            return xml
        raise ConnectionError(f"Connection failed for {url}")

    monkeypatch.setattr(p, "safe_http_get", fail_media)
    stats = p.collect_publisher(p.core.session(), "10.1038/s41586-026-00000-1", tmp_path, "springer", False)
    assert stats["missing_figure_records"] == 2
    for name in ("article.xml", "manifest.json", "audit.md", "figure_index.csv"):
        assert "fixture_secret" not in (tmp_path / name).read_text(encoding="utf-8-sig")
    audit = (tmp_path / "audit.md").read_text(encoding="utf-8")
    assert "ConnectionError while requesting official publisher API" in audit
    assert "no allowlisted absolute media address" in audit
    assert not p.allowed_publisher_url("https://api.elsevier.com:bad/content/object/eid/x", "elsevier")
    assert not p.allowed_publisher_url("https://api.elsevier.com/content/object/%2e%2e/article/x", "elsevier")

