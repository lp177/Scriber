"""FastAPI application factory for the Scriber admin dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from scriber.web import api_v1, routes

# Repository root: scriber/web/app.py -> web -> scriber package -> repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"


def create_app(bot: Any | None = None) -> FastAPI:
    """Build the FastAPI app: API under /api, built frontend (if present) at /."""
    app = FastAPI(title="Scriber", docs_url=None, redoc_url=None)
    app.state.bot = bot
    app.include_router(routes.router, prefix="/api")
    # Public token-authenticated data API (Authorization: Bearer <api-token>).
    app.include_router(api_v1.router, prefix="/api/v1")
    # Serve the built SPA only when it exists so a dev checkout without a
    # frontend build still exposes the API.
    if _FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
    return app
