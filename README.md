# ui-compare-md-pdf-parsing

Side-by-side comparison UI for PDF documents and their parsed Markdown counterparts.

Two-column layout with synced scroll: the left pane renders PDF pages as images, the right pane shows the Markdown as rendered HTML or raw source.

## Quick start

```bash
uv sync
uv run uvicorn ui.app:app --host 127.0.0.1 --port 8080
```

Open http://127.0.0.1:8080 in a browser, enter absolute paths to PDF and Markdown folders, and click **Load pairs**.

## How it works

The server scans both directories for files sharing the same stem (e.g. `report.pdf` + `report.md`) and presents them side by side. PDF pages are rendered to PNG via PyMuPDF; Markdown is converted to HTML via mistletoe and displayed with optional raw source view.

## Project structure

```
ui/
├── app.py                 # FastAPI backend
├── templates/
│   └── compare.html       # Jinja2 template (HTML + inline JS)
└── static/
    └── compare.css        # Styles
```
