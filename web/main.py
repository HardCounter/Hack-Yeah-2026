"""Minimal web app: health check plus the static frontend. API endpoints are added later."""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).resolve().parents[1] / "static"

app = FastAPI(title="AI Control Layer", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "commit": os.environ.get("GIT_SHA", "dev")}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
