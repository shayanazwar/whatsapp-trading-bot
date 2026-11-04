from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.db import SessionLocal

from app.api.routes import (
    router,
    verify_whatsapp_webhook,
    receive_whatsapp_webhook,
)
from app.db import init_db

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="KRONOS-MEXC-INTELLIGENCE",
    version="1.2.0",
    description="Independent MEXC Futures research platform. Paper/research only; no live order endpoints.",
    lifespan=lifespan,
)
app.include_router(router)

# Backward-compatible webhook path. The deployed callback in the user's logs
# POSTs to /webhook, while the API router's canonical path is /api/whatsapp/webhook.
# Both paths use the same token verification and raw-body HMAC validation.
app.add_api_route("/webhook", verify_whatsapp_webhook, methods=["GET"], include_in_schema=False)
app.add_api_route("/webhook", receive_whatsapp_webhook, methods=["POST"], include_in_schema=False)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; script-src 'self'; connect-src 'self'",
    )
    return response


@app.get("/healthz", tags=["health"])
def healthz():
    return {"status": "ok", "mode": "research_only"}


@app.get("/readyz", tags=["health"])
def readyz():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ready", "database": "reachable"}
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unreachable", "error_type": type(exc).__name__})


static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists() and (static_dir / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        requested = (static_dir / full_path).resolve()
        if full_path and static_dir.resolve() in requested.parents and requested.exists() and requested.is_file():
            return FileResponse(requested)
        return FileResponse(static_dir / "index.html")
else:
    @app.get("/", include_in_schema=False)
    def root():
        return {"application": "KRONOS-MEXC-INTELLIGENCE", "docs": "/docs", "health": "/healthz",
                "frontend": "not built; run frontend npm install && npm run build"}
