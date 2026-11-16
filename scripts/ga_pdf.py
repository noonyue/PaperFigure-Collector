"""Last-resort crop of an explicitly labelled graphical abstract in an authorized PDF."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from ga_engine import AcquisitionError
from vor_audit import normalize_doi

GA_HEADING = re.compile(r"^(?:graphical|visual)\s+abstract\s*[:.]?$", re.I)
NEXT_HEADING = re.compile(
    r"^(?:(?:fig(?:ure)?\.?\s*\d+)\b|(?:abstract|summary|highlights|key\s*words|introduction|results|methods|references)\s*[:.]?$)",
    re.I,
)


def pdf_fallback(
    path: Path,
    url: str,
    *,
    page: int | None = None,
    rect: tuple[float, float, float, float] | None = None,
    doi: str | None = None,
):
    """Return a lazy fallback. An explicit crop must remain within the labelled section."""

    def crop():
        try:
            import pymupdf as fitz
        except ImportError as exc:
            raise AcquisitionError(
                "pdf_dependency_missing", "Install the PyMuPDF optional dependency"
            ) from exc
        u = urlsplit(url)
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            raise AcquisitionError(
                "unverified_pdf_source",
                "A public HTTPS source URL is required for the authorized PDF",
            )
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise AcquisitionError(
                    "encrypted_pdf", "The PDF requires access authorization"
                )
            if doi:
                first = "\n".join(doc[i].get_text() for i in range(min(2, len(doc))))
                if normalize_doi(doi) not in re.sub(
                    r"[\s\u00ad]+", "", first.casefold()
                ):
                    raise AcquisitionError(
                        "pdf_doi_unverified",
                        "The selected PDF does not corroborate the requested DOI on its first two pages",
                    )
            if page is not None and not 1 <= page <= len(doc):
                raise ValueError("Graphical Abstract PDF page is out of bounds")
            choices = [page - 1] if page is not None else range(min(3, len(doc)))
            hits = []
            for idx in choices:
                sheet = doc[idx]
                lines = []
                for block in sheet.get_text(
                    "dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
                )["blocks"]:
                    for line in block.get("lines", []):
                        line_text = " ".join(
                            "".join(x["text"] for x in line["spans"]).split()
                        )
                        lines.append((line_text, fitz.Rect(line["bbox"])))
                for line, heading in lines:
                    if not GA_HEADING.fullmatch(line):
                        continue

                    def in_section(box):
                        if (
                            box.is_empty
                            or box.is_infinite
                            or not sheet.rect.contains(box)
                            or box.y0 < heading.y1
                        ):
                            return False
                        left, right = min(heading.x0, box.x0), max(heading.x1, box.x1)
                        # A heading in an unrelated adjacent column does not delimit the abstract drawing.
                        return not any(
                            heading.y1 < label_box.y0 < box.y1
                            and left < label_box.x1
                            and label_box.x0 < right
                            and NEXT_HEADING.match(label)
                            for label, label_box in lines
                        )

                    if rect is not None:
                        box = fitz.Rect(rect)
                        if in_section(box):
                            hits.append((idx, box))
                    else:
                        possible = []
                        for image in sheet.get_image_info():
                            box = fitz.Rect(image["bbox"])
                            if box.width >= 60 and box.height >= 40 and in_section(box):
                                possible.append(box)
                        if len(possible) == 1:
                            hits.append((idx, possible[0]))
            if len(hits) != 1:
                raise ValueError(
                    "No unique crop inside an explicit Graphical Abstract PDF section"
                )
            idx, box = hits[0]
            if box.width * box.height * 4 > 20_000_000:
                raise ValueError("Graphical Abstract PDF crop is too large")
            bitmap = doc[idx].get_pixmap(
                matrix=fitz.Matrix(2, 2), clip=box, alpha=False, annots=False
            )
            return (
                bitmap.tobytes("png"),
                url,
                {"page": idx + 1, "rect_pt": list(box), "dpi": 144},
            )

    if rect is not None and page is None:
        raise ValueError(
            "A Graphical Abstract crop rectangle requires a PDF page number"
        )
    return crop
