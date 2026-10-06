"""SQLite dashboard reads and config management. Deferred reads return 501.

Static frontend examples are available only when example_mode is explicitly enabled.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import asyncio
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, FastAPI, Path as FastApiPath, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from persistence.http_api import examples as ex
from persistence.query import ReadQueries, QueryError

Kind = Literal["prompt", "tool_use", "egress", "session", "approval", "control"]
Status = Literal["completed", "blocked", "redacted", "pending_approval", "failed"]
ActionListStatus = Literal["completed", "blocked", "redacted", "pending_approval", "failed", "pending"]
Decision = Literal["ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"]
Severity = Literal["info", "low", "medium", "high", "critical"]
Source = Literal["gateway", "finding", "alert", "verification"]
SideEffect = Literal["read", "write", "irreversible"]
SessionState = Literal["active", "ended", "halted"]
VerificationFilter = Literal["VERIFIED_SUCCESS", "FAILED_POSTCONDITIONS", "VERIFICATION_INCOMPLETE", "none"]
Adjustment = Literal["ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION"]
DecisionOutcome = Literal["decided", "failed", "dead_lettered"]
Scope = Literal["session", "run", "case", "agent"]
GroupBy = Literal["agent", "session", "case", "model", "tool", "day"]
Metric = Literal["actions", "blocked", "redacted", "detections", "input_tokens", "output_tokens", "cost_usd",
                 "interception_overhead_ms_p95"]
Bucket = Literal["1m", "5m", "1h", "1d"]

SEVERITIES = list(Severity.__args__)
BUCKET_SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"

Id = Annotated[str, FastApiPath(pattern=ID_PATTERN)]
OptId = Annotated[str | None, Query(pattern=ID_PATTERN)]
Csv = Annotated[str | None, Query(description="comma-separated values")]
Limit = Annotated[int, Query(ge=1, le=1000)]
Cursor = Annotated[str | None, Query()]
Since = Annotated[datetime | None, Query(description="inclusive, ISO 8601 with timezone")]
Until = Annotated[datetime | None, Query(description="exclusive, ISO 8601 with timezone")]


class ApiError(Exception):
    MESSAGES = {
        "bad_request": "malformed request parameter",
        "invalid_cursor": "cursor is invalid or outdated",
        "invalid_filter": "unknown filter value",
        "not_found": "resource not found",
        "method_not_allowed": "method not allowed",
        "evidence_expired": "evidence for this run has expired",
        "export_quota_exceeded": "export exceeds the configured quota",
        "store_unavailable": "evidence store is unavailable",
        "not_implemented": "endpoint is not implemented for persisted evidence",
    }

    def __init__(self, status: int, code: str, **details: Any):
        super().__init__(code)
        self.status, self.code, self.details = status, code, details


def error_response(status: int, code: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": ApiError.MESSAGES[code], "details": details or {}}})


# --- parameter helpers ------------------------------------------------------------------------

def csv(name: str, value: str | None, allowed: type) -> list[str] | None:
    """Parse a CSV filter against a Literal type; unknown values are 400 invalid_filter."""
    if value is None:
        return None
    items = [v for v in value.split(",") if v]
    if not items or any(v not in allowed.__args__ for v in items):
        raise ApiError(400, "invalid_filter", parameter=name, value=value)
    return items


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def query_time(value: datetime | None) -> str | None:
    """Match the sanitizer's microsecond precision without truncating query boundaries."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if value is not None else None


def window(since: datetime | None, until: datetime | None,
           default: timedelta | None = None, *, precise: bool = False) -> tuple[str | None, str | None]:
    for name, value in (("since", since), ("until", until)):
        if value is not None and value.tzinfo is None:
            raise ApiError(400, "bad_request", parameter=name, reason="timezone required")
    if since and until and since >= until:
        raise ApiError(400, "bad_request", parameter="since")
    if default is not None:
        until = until or datetime.now(timezone.utc)
        since = since or until - default
    if since and until and since >= until:
        raise ApiError(400, "bad_request", parameter="since")
    formatter = query_time if precise else iso
    return formatter(since), formatter(until)


# The stub cursor encodes a list offset. The real implementation encodes the keyset sort key.
def page(items: list[dict[str, Any]], limit: int, cursor: str | None) -> dict[str, Any]:
    offset = 0
    if cursor is not None:
        try:
            offset = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"==="))["o"]
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise ApiError(400, "invalid_cursor") from None
        if not isinstance(offset, int) or not 0 <= offset <= len(items):
            raise ApiError(400, "invalid_cursor")
    has_more = offset + limit < len(items)
    next_cursor = base64.urlsafe_b64encode(json.dumps({"o": offset + limit}).encode()).decode().rstrip("=")
    return {"items": items[offset:offset + limit], "next_cursor": next_cursor if has_more else None,
            "has_more": has_more}


def keep(items: list[dict[str, Any]], key: str, allowed: list[str] | str | None) -> list[dict[str, Any]]:
    """Equality/CSV filter on example rows so the stub reacts to filters."""
    if allowed is None:
        return items
    allowed = [allowed] if isinstance(allowed, str) else allowed
    return [i for i in items if i.get(key) in allowed]


def at_least(items: list[dict[str, Any]], key: str, minimum: str | None) -> list[dict[str, Any]]:
    if minimum is None:
        return items
    floor = SEVERITIES.index(minimum)
    return [i for i in items if i.get(key) is not None and SEVERITIES.index(i[key]) >= floor]


# --- routes (docs/rest.md section 4) ----------------------------------------------------------

router = APIRouter(prefix="/api/v1")


@router.get("/health")
async def health(request: Request):
    if not request.app.state.example_mode:
        status, result = await asyncio.to_thread(request.app.state.read_queries.health)
        return JSONResponse(result, status_code=status)
    return {"status": "ok", "schema_version": 3, "read_only": True, "now": ex.now()}


@router.get("/sessions")
async def list_sessions(request: Request, agent_id: OptId = None, case_id: OptId = None, run_id: OptId = None,
                        state: SessionState | None = None, verification_status: VerificationFilter | None = None,
                        min_severity: Severity | None = None, since: Since = None, until: Until = None,
                        limit: Limit = 100, cursor: Cursor = None):
    lo, hi = window(since, until)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.sessions, limit=limit, cursor=cursor,
                                       agent_id=agent_id, case_id=case_id, run_id=run_id, state=state,
                                       verification_status=verification_status, min_severity=min_severity,
                                       since=query_time(since), until=query_time(until))
    items = keep(ex.sessions(), "state", state)
    for key, value in (("agent_id", agent_id), ("case_id", case_id), ("run_id", run_id)):
        items = keep(items, key, value)
    items = [s for s in items if (lo is None or s["started_at"] >= lo) and (hi is None or s["started_at"] < hi)]
    if verification_status is not None:
        items = keep(items, "verification_status", None if verification_status == "none" else verification_status)
    return page(at_least(items, "max_severity", min_severity), limit, cursor)


@router.get("/sessions/{session_id}")
async def get_session(session_id: Id, request: Request):
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.session_detail, session_id)
    return ex.session_detail(session_id)


@router.get("/sessions/{session_id}/usage")
async def get_session_usage(session_id: Id, request: Request):
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.session_usage, session_id)
    return ex.session_usage(session_id)


@router.get("/sessions/{session_id}/verification")
async def get_verification(session_id: Id, request: Request):
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.verification, session_id)
    return ex.verification(session_id)


@router.get("/sessions/{session_id}/decisions")
async def list_decisions(session_id: Id, request: Request, plugins: Csv = None, outcomes: Csv = None,
                         decisions: Csv = None, trigger_event_id: OptId = None,
                         limit: Limit = 100, cursor: Cursor = None):
    """Control-plane decision trace of one session: every plugin decision point and failed plugin run,
    oldest first, each with its brief reasoning, factors, trigger step and linked findings/adjustments."""
    filters = {"plugins": plugins.split(",") if plugins else None,
               "outcomes": csv("outcomes", outcomes, DecisionOutcome),
               "decisions": decisions.split(",") if decisions else None,
               "trigger_event_id": trigger_event_id}
    filters = {k: v for k, v in filters.items() if v}
    if request.app.state.example_mode:
        return {"items": [], "next_cursor": None, "has_more": False, "summary": []}
    return await asyncio.to_thread(request.app.state.read_queries.decisions, session_id,
                                   limit=limit, cursor=cursor, **filters)


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: Id, request: Request):
    """One traced decision with its trigger step and linked findings."""
    if request.app.state.example_mode:
        raise ApiError(404, "not_found")
    return await asyncio.to_thread(request.app.state.read_queries.decision, decision_id)


@router.get("/trajectories/{scope}/{scope_id}")
async def get_trajectory(scope: Scope, scope_id: Id, request: Request, kinds: Csv = None, statuses: Csv = None,
                         from_seq: Annotated[int | None, Query(ge=0)] = None,
                         to_seq: Annotated[int | None, Query(ge=0)] = None,
                         since: Since = None, until: Until = None,
                         view: Literal["summary", "full"] = "summary", include_detections: bool = True,
                         limit: Limit = 1000, cursor: Cursor = None):
    kind_list, status_list = csv("kinds", kinds, Kind), csv("statuses", statuses, Status)
    if scope != "session" and (from_seq is not None or to_seq is not None):
        raise ApiError(400, "bad_request", parameter="from_seq", reason="session scope only")
    lo, hi = window(since, until)
    if from_seq is not None and to_seq is not None and from_seq > to_seq:
        raise ApiError(400, "bad_request", parameter="from_seq")
    if not request.app.state.example_mode:
        if scope != "session":
            raise ApiError(501, "not_implemented")
        return await asyncio.to_thread(request.app.state.read_queries.trajectory, scope_id,
                                       limit=limit, cursor=cursor, view=view, include_detections=include_detections,
                                       kinds=kind_list, statuses=status_list, from_seq=from_seq, to_seq=to_seq,
                                       since=query_time(since), until=query_time(until))
    result = ex.trajectory(scope, scope_id, view)
    segment = result["segments"][0]

    def row(step):
        return step["summary"] if view == "full" else step

    steps = [s for s in segment["steps"]
             if (kind_list is None or row(s)["kind"] in kind_list)
             and (status_list is None or row(s)["status"] in status_list)
             and (from_seq is None or row(s)["seq"] >= from_seq)
             and (to_seq is None or row(s)["seq"] <= to_seq)]
    if not include_detections:
        for s in steps:
            row(s)["detections"] = []
    paged = page(steps, limit, cursor)
    segment["steps"] = paged["items"]
    result.update(next_cursor=paged["next_cursor"], has_more=paged["has_more"])
    return result


@router.get("/actions")
async def list_actions(request: Request, session_id: OptId = None, run_id: OptId = None, case_id: OptId = None,
                       agent_id: OptId = None, action_id: OptId = None, name: OptId = None,
                       kinds: Csv = None, statuses: Csv = None, decisions: Csv = None, side_effects: Csv = None,
                       include_intents: bool = False, since: Since = None, until: Until = None,
                       limit: Limit = 100, cursor: Cursor = None):
    kind_list = csv("kinds", kinds, Kind)
    status_list = csv("statuses", statuses, ActionListStatus)
    decision_list = csv("decisions", decisions, Decision)
    side_effect_list = csv("side_effects", side_effects, SideEffect)
    filters = (("kind", kind_list), ("status", status_list),
               ("decision", decision_list), ("side_effect", side_effect_list),
               ("name", name), ("action_id", action_id), ("run_id", run_id), ("case_id", case_id),
               ("agent_id", agent_id))
    lo, hi = window(since, until)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(
            request.app.state.read_queries.actions, limit=limit, cursor=cursor,
            include_intents=include_intents, session_id=session_id, run_id=run_id,
            case_id=case_id, agent_id=agent_id, action_id=action_id, name=name,
            kinds=kind_list, statuses=status_list, decisions=decision_list,
            side_effects=side_effect_list, since=query_time(since), until=query_time(until))
    # Example mode: one session is listed in seq order, every session together newest first.
    items = ex.steps(session_id) if session_id else ex.all_steps()
    for key, allowed in filters:
        items = keep(items, key, allowed)
    items = [a for a in items if (lo is None or a["ts"] >= lo) and (hi is None or a["ts"] < hi)]
    return page(items, limit, cursor)


@router.get("/actions/{event_id}")
async def get_action(event_id: Id, request: Request):
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.action, event_id)
    return ex.action(event_id)


@router.get("/detections")
async def list_detections(request: Request, session_id: OptId = None, run_id: OptId = None, case_id: OptId = None,
                          agent_id: OptId = None, trigger_event_id: OptId = None, sources: Csv = None,
                          names: Csv = None, min_severity: Severity | None = None,
                          since: Since = None, until: Until = None, order: Literal["asc", "desc"] = "desc",
                          limit: Limit = 100, cursor: Cursor = None):
    source_list = csv("sources", sources, Source)
    lo, hi = window(since, until)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.detections, limit=limit, cursor=cursor,
                                       session_id=session_id, run_id=run_id, case_id=case_id, agent_id=agent_id,
                                       trigger_event_id=trigger_event_id, sources=source_list, names=names,
                                       min_severity=min_severity, since=query_time(since), until=query_time(until), order=order)
    items = keep(ex.detections(session_id), "source", source_list)
    for key, value in (("run_id", run_id), ("case_id", case_id), ("agent_id", agent_id)):
        items = keep(items, key, value)
    items = keep(items, "name", names.split(",") if names else None)
    items = keep(items, "trigger_event_id", trigger_event_id)
    items = at_least(items, "severity", min_severity)
    items = [d for d in items if (lo is None or d["ts"] >= lo) and (hi is None or d["ts"] < hi)]
    if order == "asc":
        items.reverse()
    return page(items, limit, cursor)


@router.get("/detections/{detection_id}")
async def get_detection(detection_id: Id, request: Request):
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.detection, detection_id)
    return ex.detection(detection_id)


@router.get("/catalog/detections")
async def get_catalog(request: Request):
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.detection_catalog)
    return ex.catalog()


@router.get("/metrics/usage")
async def usage_metrics(request: Request, group_by: GroupBy = "agent", agent_id: OptId = None, case_id: OptId = None,
                        since: Since = None, until: Until = None, limit: Limit = 100):
    lo, hi = window(since, until, precise=True)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.usage_report, group_by=group_by,
                                       since=lo, until=hi, limit=limit, agent_id=agent_id, case_id=case_id)
    report = ex.usage_report(group_by, lo, hi, agent_id, case_id)
    report["buckets"] = report["buckets"][:limit]
    return report


@router.get("/metrics/security")
async def security_metrics(request: Request, agent_id: OptId = None, session_id: OptId = None,
                           since: Since = None, until: Until = None,
                           top: Annotated[int, Query(ge=1, le=100)] = 10):
    lo, hi = window(since, until, precise=True)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.security_overview,
                                       since=lo, until=hi, top=top, agent_id=agent_id, session_id=session_id)
    return ex.security_overview(lo, hi, top, session_id, agent_id)


@router.get("/metrics/performance")
async def performance_metrics(request: Request, agent_id: OptId = None, session_id: OptId = None,
                              since: Since = None, until: Until = None):
    lo, hi = window(since, until, precise=True)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.performance_overview,
                                       since=lo, until=hi, agent_id=agent_id, session_id=session_id)
    return ex.performance_overview(lo, hi, session_id, agent_id)


@router.get("/metrics/timeseries")
async def timeseries(request: Request, metric: Metric, bucket: Bucket = "5m", agent_id: OptId = None, session_id: OptId = None,
                      since: Since = None, until: Until = None):
    lo, hi = window(since, until, timedelta(hours=1), precise=True)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.timeseries,
                                       metric=metric, bucket=bucket, since=lo, until=hi,
                                       session_id=session_id, agent_id=agent_id)
    start = datetime.fromisoformat(lo.replace("Z", "+00:00"))
    end = datetime.fromisoformat(hi.replace("Z", "+00:00"))
    step = BUCKET_SECONDS[bucket]
    count = math.ceil((end - start).total_seconds() / step)
    if count > 1000:
        raise ApiError(400, "bad_request", parameter="bucket", reason="more than 1000 points")
    points = []
    for i in range(count):
        value: float | None = float((i * 7) % 11)
        if metric == "cost_usd":
            value = round(value * 0.0004, 4)
        elif metric == "interception_overhead_ms_p95":
            value = None if i % 5 == 4 else round(1.5 + value / 10, 2)
        points.append({"ts": iso(start + timedelta(seconds=i * step)), "value": value})
    return {"metric": metric, "bucket": bucket, "since": lo, "until": hi, "points": points}


@router.get("/interventions")
async def list_interventions(request: Request, session_id: OptId = None, source_plugin: OptId = None, actions: Csv = None,
                             applied: bool | None = None, active: bool | None = None,
                             since: Since = None, until: Until = None, limit: Limit = 100, cursor: Cursor = None):
    action_list = csv("actions", actions, Adjustment)
    window(since, until)
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.interventions,
                                       limit=limit, cursor=cursor, session_id=session_id,
                                       source_plugin=source_plugin, actions=action_list,
                                       applied=applied, active=active,
                                       since=query_time(since), until=query_time(until))
    items = keep(keep(ex.interventions(session_id), "action", action_list), "source_plugin", source_plugin)
    if applied is not None:
        items = [i for i in items if i["applied"] is applied]
    if active is not None:
        items = [i for i in items if i["active"] is active]
    return page(items, limit, cursor)


@router.get("/system/stats")
async def system_stats(request: Request):
    if not request.app.state.example_mode:
        return await asyncio.to_thread(request.app.state.read_queries.stats)
    stats = ex.store_stats()
    # Real value: how many per-session evidence stores the read side can see (one per bound session).
    evidence_dir = request.app.state.evidence_dir
    stats["evidence_stores"] = len(list(evidence_dir.glob(EVIDENCE_GLOB))) if evidence_dir else None
    return stats


@router.get("/export/sessions/{session_id}")
async def export_session(session_id: Id, request: Request):
    if not request.app.state.example_mode:
        payload = await asyncio.to_thread(request.app.state.read_queries.export, session_id)
        return Response(payload, media_type="application/x-ndjson",
                        headers={"Content-Disposition": f'attachment; filename="audit-{session_id}.ndjson"'})
    records = [{"record_type": "session", **ex.session_detail(session_id)}]
    records += [{"record_type": "action", **ex.action(s["event_id"])["event"]} for s in ex.steps(session_id)]
    records += [{"record_type": "detection", **d} for d in ex.detections(session_id)]
    records += [{"record_type": "intervention", **i} for i in ex.interventions(session_id)]
    records += [{"record_type": "verification", **ex.verification(session_id)}]
    lines = [json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records]
    digest = hashlib.sha256("".join(lines).encode()).hexdigest()
    lines.append(json.dumps({"record_type": "export_footer", "rows": len(records), "sha256": digest,
                             "exported_at": ex.now()}, sort_keys=True, separators=(",", ":")) + "\n")
    return Response("".join(lines), media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="audit-{session_id}.ndjson"'})


@router.get("/export/actions")
async def export_actions(request: Request, kinds: Csv = None, decisions: Csv = None,
                         since: Since = None, until: Until = None, session_id: OptId = None, agent_id: OptId = None):
    kind_list, decision_list = csv("kinds", kinds, Kind), csv("decisions", decisions, Decision)
    window(since, until)
    if request.app.state.example_mode:
        rows = keep(keep(ex.all_steps(), "kind", kind_list), "decision", decision_list)
        rows = [row for row in rows if (session_id is None or row["session_id"] == session_id)
                and (agent_id is None or row["agent_id"] == agent_id)
                and (since is None or row["ts"] >= query_time(since)) and (until is None or row["ts"] < query_time(until))]
        payload = "".join(json.dumps({"record_type": "action", **ex.action(row["event_id"])["event"]}) + "\n" for row in rows)
        return Response(payload, media_type="application/x-ndjson",
                        headers={"Content-Disposition": 'attachment; filename="audit-actions.ndjson"'})
    output = await asyncio.to_thread(request.app.state.read_queries.export_actions, kinds=kind_list, decisions=decision_list,
                                     since=query_time(since), until=query_time(until), session_id=session_id, agent_id=agent_id)
    def chunks():
        try:
            while chunk := output.read(64 * 1024):
                yield chunk
        finally:
            output.close()
    return StreamingResponse(chunks(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="audit-actions.ndjson"'})


# --- app --------------------------------------------------------------------------------------

EVIDENCE_GLOB = "*.evidence.db"  # GovernedRuntime writes <runs-dir>/<session_id>.evidence.db


def create_app(cors_origins: frozenset[str] = frozenset(), docs: bool = True,
               evidence_dir: Path | None = None, *, config_service=None, example_mode=False, config_writes=False) -> FastAPI:
    """Build dashboard reads and authenticated config management (evidence remains read-only).

    ``evidence_dir`` holds per-session evidence stores, opened read-only per query.
    """
    app = FastAPI(title="Dashboard reads and configuration management", version="0.1.0",
                  description="Read-only persisted evidence and configuration management. See docs/rest.md.",
                  docs_url="/api/v1/docs" if docs else None, redoc_url=None,
                  openapi_url="/api/v1/openapi.json" if docs else None)
    app.state.evidence_dir = evidence_dir
    app.state.example_mode = example_mode
    app.state.read_queries = ReadQueries(evidence_dir)
    app.include_router(router)
    from configuration.api import install_config_api, management_put
    install_config_api(app, config_service=config_service, allow_writes=config_writes)

    @app.middleware("http")
    async def read_only(request: Request, call_next):
        if request.method not in ("GET", "HEAD") and not (
                config_writes and request.method == "PUT" and management_put(request.url.path)):
            return error_response(405, "method_not_allowed")
        path = request.url.path
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        if path.startswith("/api/v1/") and not path.startswith("/api/v1/config"):
            response.headers["X-Data-Source"] = "example" if example_mode else "persisted"
        return response

    # Registered after read_only so it runs outside it and answers CORS preflight (OPTIONS).
    if cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=sorted(cors_origins),
                           allow_methods=["GET", "HEAD", "PUT"] if config_writes else ["GET", "HEAD"],
                           allow_headers=["Authorization", "X-Admin-Token", "Content-Type"],
                           expose_headers=["ETag", "X-Data-Source"])

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.details)

    @app.exception_handler(QueryError)
    async def query_error(_: Request, exc: QueryError):
        return error_response(exc.status, exc.code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        location = first.get("loc", ())
        parameter = str(location[-1]) if location else None
        if location and location[0] == "path":
            # e.g. /trajectories/planet/x: an unknown scope is a missing resource, not a bad filter.
            return error_response(404 if first.get("type") == "literal_error" else 400,
                                  "not_found" if first.get("type") == "literal_error" else "bad_request",
                                  {"parameter": parameter})
        code = "invalid_filter" if first.get("type") in ("literal_error", "enum") else "bad_request"
        return error_response(400, code, {"parameter": parameter})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        if exc.status_code == 405:
            return error_response(405, "method_not_allowed")
        return error_response(404, "not_found")

    return app
