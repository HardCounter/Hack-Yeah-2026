"""Shared management router; evidence routes remain read-only."""
import asyncio
from contextlib import asynccontextmanager
import os
import re
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from configuration.models import (ConfigSelectionRequest, ConfigSelectionResult, ConfigSummary,
                                  ConfigUpdateResult, ErrorResponse, PolicyConfig)
from configuration.service import ConfigError, ConfigService, MAX_CONFIG_BYTES, parse_json

router = APIRouter(prefix="/api/v1", tags=["configuration"])
bearer = HTTPBearer(auto_error=False, scheme_name="ConfigManagementBearer")
ERRORS = {status: {"model": ErrorResponse} for status in (400, 401, 404, 405, 409, 413, 415, 422, 503)}


def service(request):
    return getattr(request.app.state, "config_service", None) or ConfigService()


async def administrator(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = getattr(request.app.state, "config_admin_token", None)
    if token is None:
        token = os.environ.get("CONFIG_ADMIN_TOKEN", "")
    if (not token or credential is None or credential.scheme.lower() != "bearer"
            or not secrets.compare_digest(credential.credentials.encode(), token.encode())):
        raise ConfigError(401, "unauthorized")


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
async def get_config(name: str, request: Request):
    entry = await asyncio.to_thread(service(request).get_config, name)
    return JSONResponse(entry["config"], headers={"ETag": f'"{entry["revision"]}"', "Cache-Control": "no-store"})


@router.put("/configs/{name}", response_model=ConfigUpdateResult, responses=ERRORS,
            dependencies=[Depends(administrator)],
            openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/PolicyConfig"}}}}})
async def update_config(name: str, request: Request):
    value = await body(request)
    return await asyncio.to_thread(service(request).update, name, value)


@router.put("/config-selection", response_model=ConfigSelectionResult, responses=ERRORS,
            dependencies=[Depends(administrator)],
            openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
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


def install_config_api(app, *, config_service=None, admin_token=None):
    app.state.config_service = config_service
    app.state.config_admin_token = admin_token
    app.include_router(router)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        backend = application.state.config_service or ConfigService()
        await asyncio.to_thread(backend.snapshot_for_intercept)
        async with original_lifespan(application) as state:
            yield state

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def config_methods(request: Request, call_next):
        path = request.url.path
        if path == "/api/v1/config-selection" and request.method != "PUT":
            return JSONResponse(ConfigError(405, "method_not_allowed").body(), status_code=405)
        if path.startswith("/api/v1/configs"):
            allowed = ("GET", "HEAD") if path == "/api/v1/configs" else ("GET", "HEAD", "PUT")
            if request.method not in allowed:
                return JSONResponse(ConfigError(405, "method_not_allowed").body(), status_code=405)
        return await call_next(request)

    @app.exception_handler(ConfigError)
    async def error_handler(_, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            for model in (PolicyConfig, ConfigSelectionRequest):
                model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
                components.update(model_schema.pop("$defs", {}))
                components[model.__name__] = model_schema
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
