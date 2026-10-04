"""Web app: health check, config files, the one-shot run API and the static frontend."""
import json
import os
import re
import secrets
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from intercept.auditors import Pipeline
from web import runs

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PRESETS = ROOT / "config" / "presets"  # committed lenient / standard / strict
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MAX_CONFIG_BYTES = 64_000

app = FastAPI(title="AI Control Layer", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def revalidate(request: Request, call_next):
    # Browsers may revalidate (cheap 304 via ETag) but never serve a stale frontend after a deploy.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


def custom_dir() -> Path:
    # On the server this points at the data volume, so saved configs survive a deploy.
    return Path(os.environ.get("CONFIG_DIR", ROOT / "var" / "config"))


def config_files() -> dict[str, tuple[Path, bool]]:
    files = {p.stem: (p, False) for p in custom_dir().glob("*.json")}
    files.update({p.stem: (p, True) for p in PRESETS.glob("*.json")})  # presets win a name clash
    return files


def validate(config: dict, tools: set[str]) -> None:
    """Shape checks for the fields the editor changes; auditors use the gateway's own validation."""
    def need(ok, msg):
        if not ok:
            raise HTTPException(422, msg)

    is_int = lambda v, lo, hi: type(v) is int and lo <= v <= hi
    need(isinstance(config, dict), "config must be an object")
    allowed = config.get("allowed_tools")
    need(isinstance(allowed, list) and set(allowed) <= tools, "allowed_tools must be known tools")
    need(isinstance(config.get("require_approval"), list) and set(config["require_approval"]) <= set(allowed),
         "require_approval must be a subset of allowed_tools")
    budget = config.get("budget")
    need(isinstance(budget, dict) and is_int(budget.get("tokens"), 1, 1_000_000)
         and is_int(budget.get("tool_calls"), 1, 1000), "budget.tokens and budget.tool_calls must be positive integers")
    cost = budget.get("cost_usd")
    need(cost is None or (type(cost) in (int, float) and 0 <= cost <= 1000), "budget.cost_usd must be empty or 0-1000")
    need(is_int(config.get("max_output_tokens"), 1, 32_768), "max_output_tokens must be 1-32768")
    guard = config.get("semantic_guard")
    need(isinstance(guard, dict) and type(guard.get("block_threshold")) in (int, float)
         and 0 < guard["block_threshold"] <= 1, "semantic_guard.block_threshold must be in (0, 1]")
    try:
        Pipeline(config.get("auditors"))
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"auditors: {e}")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "commit": os.environ.get("GIT_SHA", "dev")}


@app.get("/api/v1/configs")
def list_configs():
    out = []
    for name, (path, preset) in sorted(config_files().items(), key=lambda kv: (not kv[1][1], kv[0])):
        out.append({"name": name, "preset": preset,
                    "description": json.loads(path.read_text(encoding="utf-8")).get("description", "")})
    return out


@app.get("/api/v1/configs/{name}")
def get_config(name: str):
    entry = config_files().get(name)
    if not entry:
        raise HTTPException(404, "no such config")
    return json.loads(entry[0].read_text(encoding="utf-8"))


@app.put("/api/v1/configs/{name}")
async def save_config(name: str, request: Request, authorization: str = Header("")):
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token:
        raise HTTPException(503, "saving is disabled: ADMIN_TOKEN is not set")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "admin token required")
    if not NAME.fullmatch(name):
        raise HTTPException(422, "name: lowercase letters, digits, - and _, up to 40 characters")
    if (PRESETS / f"{name}.json").exists():
        raise HTTPException(409, "presets are read-only; save under another name")
    body = await request.body()
    if len(body) > MAX_CONFIG_BYTES:
        raise HTTPException(413, "config too large")
    try:
        config = json.loads(body)
    except ValueError:
        raise HTTPException(422, "body is not JSON")
    tools = set(json.loads((PRESETS / "lenient.json").read_text(encoding="utf-8"))["allowed_tools"])
    validate(config, tools)
    config["name"] = name
    folder = custom_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{name}.tmp"
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    tmp.replace(folder / f"{name}.json")  # atomic, so a reader never sees half a file
    return {"status": "saved", "name": name}


app.include_router(runs.router)
app.mount("/css", StaticFiles(directory=STATIC / "css"), name="css")
app.mount("/js", StaticFiles(directory=STATIC / "js"), name="js")
app.mount("/", StaticFiles(directory=STATIC / "html", html=True), name="html")
