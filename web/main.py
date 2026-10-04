"""Web app: health check, config files, the free-agent session API, the suite runner and the static frontend."""
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from configuration.api import install_config_api
from web import sessions, suite

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_app):
    yield
    await sessions.shutdown()  # stop every session's OpenCode server and gateway


app = FastAPI(title="AI Control Layer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


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
