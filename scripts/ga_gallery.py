"""Self-contained local HTML rendering for paper cards and scoped figures."""

from __future__ import annotations

from html import escape
from pathlib import Path
from urllib.parse import quote

from vor_audit import normalize_doi

ORDER = (
    ("graphical_abstract", "Graphical Abstract"),
    ("main", "Main figures"),
    ("extended_data", "Extended Data"),
    ("supplementary", "Supplementary"),
    ("unclassified", "Unclassified"),
)
INLINE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
EDITION = {
    "web_only": "Web only",
    "print_only": "Print only",
    "unspecified": "Edition unspecified",
}
READABLE_STATUS = {
    "downloaded": "Downloaded",
    "detected_not_accessible": "Detected · media unavailable",
    "candidate_needs_review": "Candidate · review needed",
    "not_found_in_source": "Not found in the inspected source",
    "confirmed_not_provided": "Publisher inventory confirms absence",
    "unknown": "Source unverified",
}


def relative_asset(archive: Path, output: Path, value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    file = (archive / value).resolve()
    if (
        file.is_file()
        and file.is_relative_to(archive.resolve())
        and file.is_relative_to(output.parent.resolve())
    ):
        return quote(file.relative_to(output.parent.resolve()).as_posix(), safe="/")
    return None


def figure_html(folder, output, label, caption, records, attribution=""):
    label = escape(label)
    parts = [f"<figure><h4>{label}</h4>"]
    for record in records:
        asset = record.get("asset") or record
        file = relative_asset(folder, output, asset.get("file"))
        name = record.get("edition") or asset.get("edition")
        tags = []
        if name:
            tags.append(EDITION.get(name, name))
        if asset.get("acquisition_method") == "pdf_crop":
            tags.append("pdf_crop")
        if tags:
            parts.append(f"<p class='source'>{escape(' · '.join(tags))}</p>")
        if file:
            src = escape(file, quote=True)
            ext = Path(asset["file"]).suffix.lower()
            if ext in INLINE:
                parts.append(
                    f'<a href="{src}" target="_blank" rel="noopener"><img loading="lazy" src="{src}" alt="{label}"></a>'
                )
            else:
                parts.append(f'<a href="{src}">Open source asset ({escape(ext)})</a>')
        elif record.get("reason") or record.get("error"):
            parts.append(
                f'<p class="source">{escape(record.get("reason") or record.get("error"))}</p>'
            )
    if caption or attribution:
        parts.append(
            f"<figcaption>{escape(caption)}"
            + (f"<p>{escape(attribution)}</p>" if attribution else "")
            + "</figcaption>"
        )
    parts.append("</figure>")
    return "\n".join(parts)


def render(output: Path, articles: list[tuple[Path, dict]]) -> None:
    output = output.resolve()
    content = [
        """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>PaperFigure-Collector · Figure gallery</title>
<style>
body{font:15px/1.55 "Segoe UI",sans-serif;margin:0;color:#15313b;background:#f4f7f7}
main{width:min(1120px,calc(100% - 32px));margin:24px auto}
.paper-card{background:#fff;border:1px solid #d7e3e3;border-radius:10px;margin:18px 0;padding:18px}
h1,h2,h3,h4{margin:0 0 10px}section{border-top:1px solid #dde6e6;padding:14px 0}
figure{margin:0 0 17px}img{display:block;max-width:100%;max-height:80vh;height:auto;object-fit:contain;background:#f7f9f9}
figcaption{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:10px}
.source{color:#45616b;font-size:.9em}a{color:#005f55}p{margin:0 0 10px}
</style><main><h1>Article figure gallery</h1>"""
    ]
    if output.name != "index.html" and (output.parent / "index.html").is_file():
        content.append('<p><a href="index.html">Search and filter all figures</a></p>')
    for folder, manifest in articles:
        metadata = manifest.get("metadata", {})
        figure_rows = manifest.get("figures", [])
        ga = manifest.get("graphical_abstract") or {}
        title = str(metadata.get("title") or metadata.get("doi") or "Article")
        doi = normalize_doi(metadata.get("doi"))
        safe_title = escape(title)
        if doi:
            safe_title = f'<a href="https://doi.org/{quote(doi, safe="/()")}" target="_blank" rel="noopener noreferrer">{safe_title}</a>'
        content.append(f'<article class="paper-card"><h2>{safe_title}</h2>')
        for category, label in ORDER:
            rows = [f for f in figure_rows if f.get("category") == category]
            if category != "graphical_abstract" and not rows:
                continue
            content.append(f'<section data-category="{category}"><h3>{label}</h3>')
            if category == "graphical_abstract" and ga.get("records"):
                for row in ga["records"]:
                    content.append(
                        figure_html(
                            folder,
                            output,
                            label,
                            row.get("caption") or "",
                            [row],
                            row.get("attribution") or "",
                        )
                    )
            elif category == "graphical_abstract" and not rows:
                status = ga.get("status")
                label = (
                    READABLE_STATUS.get(status, status)
                    if status
                    else "No graphical abstract in this archive"
                )
                content.append(f'<p class="source">{escape(label)}</p>')
                if ga.get("reason"):
                    content.append(f'<p class="source">{escape(ga["reason"])}</p>')
            else:
                for row in rows:
                    content.append(
                        figure_html(
                            folder,
                            output,
                            row.get("label") or label,
                            row.get("caption") or "",
                            row.get("images") or [],
                            row.get("attribution") or "",
                        )
                    )
            content.append("</section>")
        content.append("</article>")
    content.append("</main></html>")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(content), encoding="utf-8")
