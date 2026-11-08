"""->FastAPI application: routers, error format, security headers, static UI."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, db
from .api import routes_accounts, routes_admin, routes_audit, routes_auth, routes_money
from .config import get_settings
from .errors import LedgerError

STATIC = Path(__file__).resolve().parent.parent / "static"
log = logging.getLogger("ledgerline")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    get_settings()          # fail fast on bad production config
    db.init_pool()
    yield
    db.close_pool()


app = FastAPI(
    title="LedgerLine API",
    version=__version__,
    description="Money movement with a ledger you can prove. Every money-moving POST needs an "
                "Idempotency-Key header. See docs/API.md for who may call what.",
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    path = request.url.path
    if path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    if not path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = CSP
    return response


@app.exception_handler(LedgerError)
async def ledger_error(request: Request, exc: LedgerError):
    return JSONResponse(status_code=exc.http_status, content=exc.to_body(), headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    fields = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", []) if part != "body")
        message = str(err.get("msg", "invalid")).removeprefix("Value error, ")
        fields.append({"field": loc, "message": message})
    return JSONResponse(status_code=422, content={"error": {
        "code": "VALIDATION_FAILED", "message": "; ".join(f"{f['field']}: {f['message']}" for f in fields),
        "fields": fields}})


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": code, "message": str(exc.detail)}},
                        headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": {
        "code": "INTERNAL_ERROR",
        "message": "internal error; it is safe to retry with the same Idempotency-Key "
                   "(a retry can never move money twice)"}})


for router in (routes_auth.router, routes_accounts.router, routes_money.router, routes_audit.router,
               routes_admin.router):
    app.include_router(router)


@app.get("/api/health", tags=["meta"])
def health():
    """Public. The chain head is a commitment, published on purpose: anyone can
    note it down and later check that history before it was never rewritten."""
    with db.read_transaction() as cur:
        cur.execute("SELECT last_seq, last_hash FROM ledger_head WHERE id = 1")
        head = cur.fetchone()
    return {"ok": True, "version": __version__, "ledger_head": {"seq": head["last_seq"], "hash": head["last_hash"]}}


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")
