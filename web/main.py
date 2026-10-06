"""Web app: health check, config files, the free-agent session API, the suite runner and the static frontend."""
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from configuration.api import install_config_api
from web import sessions, suite
from web.security import RequestBudget, browser_origin_allowed

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app):
    mutation_budget.requests.clear()
    session_budget.requests.clear()
    await sessions.startup()
    try:
        yield
    finally:
        await sessions.shutdown()  # stop every session's OpenCode server and gateway


app = FastAPI(title="AI Control Layer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
mutation_budget = RequestBudget("WEB_MUTATION_RATE_LIMIT", 120)
session_budget = RequestBudget("SESSION_START_RATE_LIMIT", 10)


def error_response(status, code, message, details=None, headers=None):
    return JSONResponse({"error": {"code": code, "message": message, "details": details or {}}},
                        status_code=status, headers=headers)


@app.exception_handler(HTTPException)
async def http_error(_request, exc):
    code = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 409: "session_busy",
            429: "rate_limit_exceeded", 503: "service_unavailable", 504: "session_timeout"}.get(exc.status_code, "http_error")
    return error_response(exc.status_code, code, exc.detail if isinstance(exc.detail, str) else "request failed",
                          headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc):
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    return error_response(422, "invalid_request", "invalid request fields", {"fields": fields})


@app.middleware("http")
async def browser_mutations(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not request.url.path.startswith("/api/v1/config"):
        if not browser_origin_allowed(request, "WEB_ALLOWED_ORIGINS"):
            return error_response(403, "origin_not_allowed", "browser origin is not allowed to perform this action")
        budget = session_budget if request.url.path == "/opencode-wrapper/api/sessions" and request.method == "POST" else mutation_budget
        if not budget.take():
            return error_response(429, "rate_limit_exceeded", "request rate limit exceeded; retry in a minute",
                                  headers={"Retry-After": "60"})
    return await call_next(request)


@app.middleware("http")
async def revalidate(request: Request, call_next):
    # Browsers may revalidate (cheap 304 via ETag) but never serve a stale frontend after a deploy.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok", "commit": os.environ.get("GIT_SHA", "dev")}


install_config_api(app)
app.include_router(sessions.router)
app.include_router(suite.router)
app.mount("/css", StaticFiles(directory=STATIC / "css"), name="css")
app.mount("/js", StaticFiles(directory=STATIC / "js"), name="js")
app.mount("/", StaticFiles(directory=STATIC / "html", html=True), name="html")
