import importlib.util
import json
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'collect_figures.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('collector', SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

JATS = b'''<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
 <front><article-meta><abstract abstract-type="graphical"><fig id="ga">
  <graphic xlink:href="abstract.png"/></fig></abstract></article-meta></front>
 <body><sec><title>Results</title><fig id="f1"><label>Fig. 1</label><caption><p>Actual main result.</p></caption>
  <alternatives><graphic xlink:href="f1-web.jpg"/><graphic xlink:href="f1-print.tif"/></alternatives>
 </fig></sec></body>
 <floats-group><fig id="f2"><label>Fig. 2</label><caption><p>Another main panel.</p></caption>
 <graphic xlink:href="f2.png"/></fig>
 <fig id="ed1"><label>Extended Data Fig. 1</label><graphic xlink:href="ed1.png"/></fig>
 <fig id="sf1"><label>Supplementary Fig. S1</label><graphic xlink:href="s1.png"/></fig></floats-group>
</article>'''


def png(path):
    img = Image.new('RGB', (50, 30))
    img.save(path)


def test_figures_categories_and_alternatives():
    figures = mod.parse_jats(JATS)
    assert len(figures) == 4
    assert [f.category for f in figures] == ['graphical_abstract', 'main', 'main', 'extended_data']
    assert figures[1].references == ['f1-print.tif']
    assert figures[1].caption == 'Actual main result.'
    assert mod.parse_jats(JATS, include_supplementary=True)[-1].category == 'supplementary'


def test_local_files_and_reporting(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    for filename in ['abstract.png', 'f2.png', 'ed1.png']:
        png(media / filename)
    # TIFF preferred but absent; web JPG variant is available as a fallback.
    Image.new('RGB', (40, 30)).save(media / 'f1-web.jpg')
    figures = mod.parse_jats(JATS)
    mod.collect_figures(figures, tmp_path / 'paper', mod.session(), [], media)
    stats = mod.write_outputs(tmp_path / 'paper', figures, {'source': 'test_fixture'}, [])
    assert stats['expected_figure_records'] == 4
    assert stats['downloaded_media_files'] == 4
    assert stats['missing_figure_records'] == 0
    assert stats['complete']
    record = json.loads((tmp_path / 'paper' / 'manifest.json').read_text())
    assert record['figures'][1]['status'] == 'downloaded'
    assert record['figures'][1]['images'][0]['file'].endswith('.jpg')
    assert (tmp_path / 'paper' / 'figure_index.csv').exists()
    assert (tmp_path / 'paper' / 'gallery.html').exists()


def test_pmc_filename_matching():
    u = 'https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/panel.jpg?md5=xyz'
    assert mod.match_reference('panel', [u]) == u
    assert mod.match_reference('missing', [u]) is None
    assert not mod.allowed_pmc_url('https://bad.example/media.jpg')


def test_missing_asset_and_gap_review(tmp_path):
    xml = b'''<article xmlns:xlink="http://www.w3.org/1999/xlink">
      <front><article-meta><counts><fig-count count="3"/></counts></article-meta></front>
      <body><fig id="a"><label>Figure 1</label><graphic xlink:href="one.png"/></fig>
      <fig id="b"><label>Figure 3</label><graphic xlink:href="three.png"/></fig></body></article>'''
    figures = mod.parse_jats(xml)
    review = mod.inspect_jats_completeness(xml, figures)
    assert any('numbering' in r for r in review)
    assert any('declares 3' in r for r in review)
    media = tmp_path / 'media'
    media.mkdir()
    png(media / 'one.png')
    mod.collect_figures(figures, tmp_path / 'p', mod.session(), [], media)
    stats = mod.write_outputs(tmp_path / 'p', figures, {'audit_notes': review}, [])
    assert stats['missing_figure_records'] == 1
    assert not stats['complete']
    assert 'Needs review' in (tmp_path / 'p' / 'audit.md').read_text()
