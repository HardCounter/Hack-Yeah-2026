"""Shared management router; evidence routes remain read-only."""
import asyncio
from contextlib import asynccontextmanager
import re
import os
import secrets
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from configuration.models import (ConfigSelectionRequest, ConfigSelectionResult, ConfigSummary,
                                  ConfigUpdateResult, ErrorResponse, PolicyConfig)
from configuration.service import ConfigError, ConfigService, MAX_CONFIG_BYTES, parse_json
from web.security import RequestBudget, browser_origin_allowed

router = APIRouter(prefix="/api/v1", tags=["configuration"])
ERRORS = {status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 405, 409, 413, 415, 422, 429, 503)}
ADMIN_SECURITY = [{"AdminToken": []}, {"AdminBearer": []}]


def service(request):
    return getattr(request.app.state, "config_service", None) or ConfigService(
        read_only=not request.app.state.config_writes_enabled)


async def body(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ConfigError(415, "unsupported_media_type")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_CONFIG_BYTES:
            raise ConfigError(413, "config_too_large")
        data.extend(chunk)
    return parse_json(bytes(data))


@router.get("/configs", response_model=list[ConfigSummary], responses=ERRORS)
@router.head("/configs", include_in_schema=False)
async def list_configs(request: Request):
    return await asyncio.to_thread(service(request).list_configs)


@router.get("/configs/{name}", response_model=PolicyConfig, responses=ERRORS)
@router.head("/configs/{name}", include_in_schema=False)
async def get_config(name: str, request: Request, view: Literal["saved", "active"] = "saved"):
    backend = service(request)
    entry = await asyncio.to_thread(backend.get_active_config if view == "active" else backend.get_config, name)
    return JSONResponse(entry["config"], headers={"ETag": f'"{entry["revision"]}"', "Cache-Control": "no-store"})


@router.put("/configs/{name}", response_model=ConfigUpdateResult, responses=ERRORS,
            openapi_extra={"security": ADMIN_SECURITY, "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/PolicyConfig"}}}}})
async def update_config(name: str, request: Request):
    value = await body(request)
    return await asyncio.to_thread(service(request).update, name, value)


@router.put("/config-selection", response_model=ConfigSelectionResult, responses=ERRORS,
            openapi_extra={"security": ADMIN_SECURITY, "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/ConfigSelectionRequest"}}}}})
async def select_config(request: Request):
    value = await body(request)
    try:
        selected = ConfigSelectionRequest.model_validate(value)
    except ValidationError:
        raise ConfigError(422, "invalid_config", ["name", "revision"]) from None
    return await asyncio.to_thread(service(request).select, selected.name, selected.revision)


def management_put(path):
    return path == "/api/v1/config-selection" or bool(re.fullmatch(r"/api/v1/configs/[^/]+", path))


def install_config_api(app, *, config_service=None, allow_writes=True):
    if not allow_writes and isinstance(config_service, ConfigService):
        config_service = ConfigService(config_service.directory, presets_dir=config_service.presets_dir, read_only=True)
    app.state.config_service = config_service
    app.state.config_writes_enabled = allow_writes
    write_budget = RequestBudget("CONFIG_WRITE_RATE_LIMIT", 30)
    app.include_router(router)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        if allow_writes:
            backend = application.state.config_service or ConfigService()
            await asyncio.to_thread(backend.snapshot_for_intercept)
        async with original_lifespan(application) as state:
            yield state

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def config_methods(request: Request, call_next):
        path = request.url.path
        if request.method == "PUT" and management_put(path):
            if not allow_writes:
                return JSONResponse(ConfigError(405, "method_not_allowed").body(), status_code=405)
            if not browser_origin_allowed(request, "CONFIG_ALLOWED_ORIGINS"):
                return JSONResponse(ConfigError(403, "origin_not_allowed").body(), status_code=403)
            secret = os.environ.get("CONFIG_ADMIN_TOKEN", "")
            if not secret:
                return JSONResponse(ConfigError(503, "config_writes_disabled").body(), status_code=503)
            if not write_budget.take():
                return JSONResponse(ConfigError(429, "rate_limit_exceeded").body(), status_code=429,
                                    headers={"Retry-After": "60"})
            authorization = request.headers.get("authorization", "").split(" ", 1)
            bearer = authorization[1] if len(authorization) == 2 and authorization[0].lower() == "bearer" else ""
            supplied = request.headers.get("x-admin-token", "") or bearer
            if not secrets.compare_digest(supplied.encode(), secret.encode()):
                return JSONResponse(ConfigError(401, "admin_token_required").body(), status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        if path == "/api/v1/config-selection" and request.method != "PUT":
            return JSONResponse(ConfigError(405, "method_not_allowed").body(), status_code=405)
        if path.startswith("/api/v1/configs"):
            allowed = ("GET", "HEAD") if path == "/api/v1/configs" else ("GET", "HEAD", "PUT")
            if request.method not in allowed:
                return JSONResponse(ConfigError(405, "method_not_allowed").body(), status_code=405)
        return await call_next(request)

    @app.exception_handler(ConfigError)
    async def error_handler(_, exc):
        return JSONResponse(exc.body(), status_code=exc.status)

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            schema["components"].setdefault("securitySchemes", {}).update({
                "AdminToken": {"type": "apiKey", "in": "header", "name": "X-Admin-Token"},
                "AdminBearer": {"type": "http", "scheme": "bearer"},
            })
            for model in (PolicyConfig, ConfigSelectionRequest):
                model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
                components.update(model_schema.pop("$defs", {}))
                components[model.__name__] = model_schema
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
