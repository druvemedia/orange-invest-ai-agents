"""Minimal web UI: one static page that uses the investigations API."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)
PAGE = Path(__file__).resolve().parents[2] / "static" / "index.html"


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={
        # The page loads nothing external and inserts all data as text, never as HTML.
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
    })
