#!/usr/bin/env python3
"""
Compare PDFs with parsed Markdown side by side.

Run:
  uv run uvicorn ui.app:app --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import difflib
import json
import urllib.parse
from pathlib import Path

import fitz
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mistletoe import Document
from mistletoe.html_renderer import HTMLRenderer

app = FastAPI(title="PDF vs Markdown compare")
_BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")


def _parse_dir_param(value: str | None, label: str) -> Path:
    if not value or not value.strip():
        raise HTTPException(status_code=400, detail=f"Missing {label}")
    p = Path(value.strip()).expanduser().resolve()
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"{label} is not a directory: {p}")
    return p


def _is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _norm_text(s: str) -> str:
    return " ".join(s.lower().split())


def _plain_from_html_fragment(fragment: str) -> str:
    return BeautifulSoup(fragment, "html.parser").get_text(separator=" ", strip=True)


def _norm_rect(x0: float, y0: float, x1: float, y1: float, pw: float, ph: float) -> dict[str, float]:
    return {
        "x0": max(0.0, min(1.0, x0 / pw)),
        "y0": max(0.0, min(1.0, y0 / ph)),
        "x1": max(0.0, min(1.0, x1 / pw)),
        "y1": max(0.0, min(1.0, y1 / ph)),
    }


def _block_center_y(b: dict) -> float:
    _, y0, _, y1 = b["bbox"]
    return (y0 + y1) / 2


def _extract_pdf_regions(pdf_path: Path) -> list[dict]:
    """One region per logical unit: tables merged via find_tables(), other text blocks, images."""
    doc = fitz.open(pdf_path)
    regions: list[dict] = []
    pdf_idx = 0
    try:
        for page_idx in range(doc.page_count):
            page = doc.load_page(page_idx)
            pw = page.rect.width
            ph = page.rect.height
            if pw <= 0 or ph <= 0:
                continue

            tables = page.find_tables()
            table_rects: list[fitz.Rect] = []
            for tab in tables.tables:
                table_rects.append(fitz.Rect(tab.bbox))

            def _inside_table(block_bbox: tuple) -> int | None:
                br = fitz.Rect(block_bbox)
                for ti, tr in enumerate(table_rects):
                    if tr.contains(br) or br.intersects(tr):
                        overlap = br & tr
                        if overlap.is_empty:
                            continue
                        overlap_area = overlap.width * overlap.height
                        block_area = br.width * br.height
                        if block_area > 0 and overlap_area / block_area > 0.5:
                            return ti
                return None

            for ti, tab in enumerate(tables.tables):
                all_cells = []
                for row in tab.extract():
                    for cell in row:
                        if cell:
                            all_cells.append(cell)
                t = " ".join(all_cells).strip()
                x0, y0, x1, y1 = tab.bbox
                regions.append(
                    {
                        "pdf_idx": pdf_idx,
                        "page": page_idx,
                        "norm": _norm_text(t),
                        "preview": t[:800],
                        "rect": _norm_rect(x0, y0, x1, y1, pw, ph),
                        "kind": "table",
                        "sort_y": y0,
                    }
                )
                pdf_idx += 1

            td = page.get_text("dict")
            for b in td.get("blocks", []):
                bt = b.get("type")
                if bt == 1:
                    if _inside_table(b["bbox"]) is not None:
                        continue
                    x0, y0, x1, y1 = b["bbox"]
                    regions.append(
                        {
                            "pdf_idx": pdf_idx,
                            "page": page_idx,
                            "norm": "",
                            "preview": "(PDF image)",
                            "rect": _norm_rect(x0, y0, x1, y1, pw, ph),
                            "kind": "image",
                            "sort_y": y0,
                        }
                    )
                    pdf_idx += 1
                    continue
                if bt != 0:
                    continue
                if _inside_table(b["bbox"]) is not None:
                    continue
                parts: list[str] = []
                for line in b.get("lines", []):
                    for sp in line.get("spans", []):
                        parts.append(sp.get("text") or "")
                t = " ".join(parts).strip()
                if not t:
                    continue
                x0, y0, x1, y1 = b["bbox"]
                regions.append(
                    {
                        "pdf_idx": pdf_idx,
                        "page": page_idx,
                        "norm": _norm_text(t),
                        "preview": t[:800],
                        "rect": _norm_rect(x0, y0, x1, y1, pw, ph),
                        "kind": "text",
                        "sort_y": y0,
                    }
                )
                pdf_idx += 1
    finally:
        doc.close()

    regions.sort(key=lambda r: (r["page"], r["sort_y"]))
    for i, r in enumerate(regions):
        r["pdf_idx"] = i
        r.pop("sort_y", None)
    return regions


def _score_md_pdf(md_norm: str, pdf_norm: str) -> float:
    if not md_norm or not pdf_norm:
        return 0.0
    if len(md_norm) < 24 or len(pdf_norm) < 24:
        if md_norm in pdf_norm or pdf_norm in md_norm:
            return 0.92
        return difflib.SequenceMatcher(None, md_norm, pdf_norm).ratio()
    return difflib.SequenceMatcher(None, md_norm[:1600], pdf_norm[:1600]).ratio()


def _match_md_to_pdf(md_blocks: list[dict], pdf_regions: list[dict]) -> list[dict]:
    cursor = 0
    out: list[dict] = []
    window = 80
    for md in md_blocks:
        bid = md["id"]
        md_norm = md["norm"]
        md_preview = md.get("preview") or ""
        if not md_norm:
            out.append(
                {
                    "id": bid,
                    "matched": False,
                    "md_preview": md_preview,
                    "pdf_preview": None,
                    "page": None,
                    "rect": None,
                    "pdf_idx": None,
                }
            )
            continue
        best_j: int | None = None
        best_score = 0.0
        end = min(cursor + window, len(pdf_regions))
        for j in range(cursor, end):
            pn = pdf_regions[j]["norm"]
            if not pn:
                continue
            sc = _score_md_pdf(md_norm, pn)
            if sc > best_score:
                best_score = sc
                best_j = j
        nlen = len(md_norm)
        if nlen < 20:
            thresh = 0.52
        elif nlen < 80:
            thresh = 0.44
        else:
            thresh = 0.36
        if best_score >= thresh and best_j is not None:
            pb = pdf_regions[best_j]
            out.append(
                {
                    "id": bid,
                    "matched": True,
                    "md_preview": md_preview,
                    "pdf_preview": pb["preview"],
                    "page": pb["page"],
                    "rect": pb["rect"],
                    "pdf_idx": pb["pdf_idx"],
                }
            )
            cursor = best_j + 1
        else:
            out.append(
                {
                    "id": bid,
                    "matched": False,
                    "md_preview": md_preview,
                    "pdf_preview": None,
                    "page": None,
                    "rect": None,
                    "pdf_idx": None,
                }
            )
    return out


def _render_markdown_blocked(text: str) -> tuple[str, list[dict]]:
    """One wrapper per top-level AST block (whole table, whole list, paragraph, heading, ...)."""
    doc = Document(text)
    fragments: list[str] = []
    meta: list[dict] = []
    with HTMLRenderer() as renderer:
        for i, child in enumerate(doc.children):
            bid = f"b{i}"
            inner = renderer.render(child)
            plain = _plain_from_html_fragment(inner)
            fragments.append(
                f'<div class="md-block" id="md-{bid}" data-block-id="{bid}">{inner}</div>'
            )
            meta.append(
                {
                    "id": bid,
                    "norm": _norm_text(plain),
                    "preview": plain[:500] if plain else "",
                }
            )
    return "\n".join(fragments), meta


def _rewrite_md_images(soup: BeautifulSoup, md_root: Path, stem: str) -> None:
    encoded_stem = urllib.parse.quote(stem, safe="")
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.startswith(("http://", "https://", "data:")):
            continue
        rel = urllib.parse.unquote(src.split("?", 1)[0])
        if rel.startswith("/"):
            continue
        rel_q = urllib.parse.quote(rel, safe="/")
        img["src"] = (
            f"/api/asset?md_dir={urllib.parse.quote(str(md_root), safe='')}"
            f"&stem={encoded_stem}&rel={rel_q}"
        )


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    pdf_dir: str | None = None,
    md_dir: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "pdf_dir_json": json.dumps(pdf_dir or ""),
            "md_dir_json": json.dumps(md_dir or ""),
        },
    )


@app.get("/api/pairs")
def api_pairs(
    pdf_dir: str = Query(..., description="Absolute path to folder with PDFs"),
    md_dir: str = Query(..., description="Absolute path to folder with .md and assets"),
) -> dict:
    pdf_root = _parse_dir_param(pdf_dir, "pdf_dir")
    md_root = _parse_dir_param(md_dir, "md_dir")

    pdf_stems = {p.stem for p in pdf_root.glob("*.pdf") if p.is_file()}
    md_stems = {p.stem for p in md_root.glob("*.md") if p.is_file()}
    stems = sorted(pdf_stems & md_stems)
    return {"stems": stems}


@app.get("/api/pdf-info/{stem}")
def api_pdf_info(
    stem: str,
    pdf_dir: str = Query(...),
) -> dict:
    pdf_root = _parse_dir_param(pdf_dir, "pdf_dir")
    pdf_path = (pdf_root / f"{stem}.pdf").resolve()
    if not _is_under(pdf_root, pdf_path) or not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="PDF not found")

    doc = fitz.open(pdf_path)
    try:
        count = doc.page_count
    finally:
        doc.close()
    return {"page_count": count}


@app.get("/api/pdf-page/{stem}/{page_index}")
def api_pdf_page(
    stem: str,
    page_index: int,
    pdf_dir: str = Query(...),
) -> Response:
    pdf_root = _parse_dir_param(pdf_dir, "pdf_dir")
    pdf_path = (pdf_root / f"{stem}.pdf").resolve()
    if not _is_under(pdf_root, pdf_path) or not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="PDF not found")

    doc = fitz.open(pdf_path)
    try:
        if page_index < 0 or page_index >= doc.page_count:
            raise HTTPException(status_code=404, detail="Page out of range")
        page = doc.load_page(page_index)
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png = pix.tobytes("png")
    finally:
        doc.close()

    return Response(content=png, media_type="image/png")


@app.get("/api/render/{stem}")
def api_render(
    stem: str,
    md_dir: str = Query(...),
    pdf_dir: str | None = Query(
        None,
        description="If set, PDF text geometry is used to align MD blocks to regions",
    ),
) -> dict:
    md_root = _parse_dir_param(md_dir, "md_dir")
    md_path = (md_root / f"{stem}.md").resolve()
    if not _is_under(md_root, md_path) or not md_path.is_file():
        raise HTTPException(status_code=404, detail="Markdown not found")

    text = md_path.read_text(encoding="utf-8", errors="replace")
    html_blob, md_meta = _render_markdown_blocked(text)
    soup = BeautifulSoup(html_blob, "html.parser")
    _rewrite_md_images(soup, md_root, stem)

    anchors: list[dict] = []
    pdf_regions_out: list[dict] = []
    if pdf_dir and pdf_dir.strip():
        pdf_root = _parse_dir_param(pdf_dir, "pdf_dir")
        pdf_path = (pdf_root / f"{stem}.pdf").resolve()
        if not _is_under(pdf_root, pdf_path) or not pdf_path.is_file():
            raise HTTPException(status_code=404, detail="PDF not found for alignment")
        pdf_regions = _extract_pdf_regions(pdf_path)
        anchors = _match_md_to_pdf(md_meta, pdf_regions)
        pdf_regions_out = [
            {
                "pdf_idx": r["pdf_idx"],
                "page": r["page"],
                "rect": r["rect"],
                "preview": r["preview"][:400],
            }
            for r in pdf_regions
        ]

    return {
        "html": str(soup),
        "raw": text,
        "anchors": anchors,
        "pdf_regions": pdf_regions_out,
    }


@app.get("/api/asset")
def api_asset(
    md_dir: str = Query(...),
    stem: str = Query(...),
    rel: str = Query(..., description="Path relative to the .md file directory"),
) -> Response:
    md_root = _parse_dir_param(md_dir, "md_dir")
    md_path = (md_root / f"{stem}.md").resolve()
    if not _is_under(md_root, md_path) or not md_path.is_file():
        raise HTTPException(status_code=404, detail="Markdown not found")

    rel_clean = urllib.parse.unquote(rel)
    if ".." in Path(rel_clean).parts:
        raise HTTPException(status_code=400, detail="Invalid path")

    asset_path = (md_path.parent / rel_clean).resolve()
    if not _is_under(md_root, asset_path) or not asset_path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")

    suffix = asset_path.suffix.lower()
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }.get(suffix, "application/octet-stream")

    return Response(content=asset_path.read_bytes(), media_type=media)
