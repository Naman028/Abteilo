"""FastAPI application factory."""

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from abteilo import admin, assistant, graph, me, overview, reports
from abteilo.ai.tools import TOOLS
from abteilo.config import get_settings
from abteilo.db import transaction
from abteilo.errors import AppError, app_error_handler, unhandled_error_handler, validation_error_handler
from abteilo.modules import documents, engineering, finance, hr, it
from abteilo.templates import DEPARTMENT_TEMPLATES, OWNER_TOOLS

MAX_BODY_BYTES = 2 * 1024 * 1024
# Every /api route lives on one of these routers; the security tests enumerate them from here.
API_ROUTERS = (me.router, engineering.router, finance.router, hr.router, it.router, documents.router,
               assistant.router, reports.router, admin.router, overview.router, graph.router)
log = logging.getLogger("abteilo.access")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';"
                               " frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def _check_templates() -> None:
    """Fail fast at startup if a template names a tool that does not exist."""
    for template in DEPARTMENT_TEMPLATES.values():
        missing = set(template.tools) - TOOLS.keys()
        if missing:
            raise RuntimeError(f"Template {template.code} references unknown tools: {sorted(missing)}")
    if set(OWNER_TOOLS) - TOOLS.keys():
        raise RuntimeError("Owner workspace references unknown tools")


def create_app() -> FastAPI:
    settings = get_settings()
    _check_templates()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = FastAPI(title="Abteilo", version="0.1.0", docs_url="/api/docs" if settings.is_development else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if settings.is_development else None)

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return JSONResponse({"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body too large.",
                                           "request_id": request.state.request_id}}, status_code=413)
        start = time.monotonic()
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        # Structured access log: method, path template-free path, status, duration. No bodies, no query strings.
        log.info('{"request_id":"%s","method":"%s","path":"%s","status":%d,"ms":%d}', request.state.request_id,
                 request.method, request.url.path, response.status_code, int((time.monotonic() - start) * 1000))
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    for router in API_ROUTERS:
        app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        try:
            with transaction() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ready"}

    if settings.frontend_dist and Path(settings.frontend_dist).is_dir():
        dist = Path(settings.frontend_dist).resolve()
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"error": {"code": "NOT_FOUND", "message": "Not found."}}, status_code=404)
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
