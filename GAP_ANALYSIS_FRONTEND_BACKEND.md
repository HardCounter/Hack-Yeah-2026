# Comprehensive Gap Analysis: Frontend vs. Backend

**Project:** Sentinel Interlock — AI Control Layer  
**Date:** 2026-10-04  
**Scope:** Integration, API contracts, routing, error handling, performance, and security between static frontend assets and Python backend services.

---

## 1. Executive Summary & Architecture Layout

The platform consists of two distinct backend processes exposed to clients through a Caddy reverse proxy:

```
                          Client Browser
                                │
                                ▼
                       Caddy (:80 / :443)
                         │            │
      /api/v1/configs/*  │            │  /api/v1/* (evidence reads)
      /api/v1/config-sel │            │  (excluding configs)
      /opencode-wrapper/ │            │
      /healthz, static   │            │
                         ▼            ▼
                     app:8000      api:8790
                  (web.main:app) (persistence.http_api)
                  FastAPI        FastAPI (read-only SQLite)
```

1. **`app:8000` (`web.main:app`)**:
   - Serves static assets (`/`, `/css`, `/js`, `/opencode-wrapper/`).
   - Serves `/healthz` (service health and git commit).
   - Serves configuration management APIs (`GET/PUT /api/v1/configs`, `PUT /api/v1/config-selection`) via `configuration/api.py`.
   - Manages interactive OpenCode sessions (`/opencode-wrapper/api/*`) via `web/sessions.py`.
2. **`api:8790` (`persistence.http_api`)**:
   - Read-only SQLite evidence store interface for recorded sessions, actions, trajectories, verification, and audit logs.
   - Deployed without example mode in production (`compose.yaml`).

---

## 2. Matrix of Holes and Discrepancies

| # | Component / Flow | Frontend Location | Backend Location | Severity | Description |
|---|---|---|---|---|---|
| **1** | **Tests Tab** | `static/js/tests.js` | `web/suite.py`, `tests/control_layer` | **PARTIALLY RESOLVED** | The guardrail test suite execution is now wired to real pytest runs via `POST /api/suite/runs` and `GET /api/suite/runs/latest`. However, bottom outcome replays (`SCEN`) remain client-side canned scripts rather than backend verification checks. |
| **2** | **501 Not Implemented Fallbacks** | `static/js/metrics.js` (`derive`), `static/js/mock.js` | `persistence/http_api/app.py` | **MEDIUM** | In normal mode, `/metrics/security`, `/metrics/usage`, `/metrics/performance`, and `/detections` return `501`. `metrics.js` falls back to `derive(rows)` and `Mock.auditorRuns` to supply simulated stand-in metrics for runs, p50, and p95 rather than displaying empty dashes. |
| **3** | **Risk Calculation Duplication & Drift** | `static/js/riskmap.js`, `static/js/app.js` | `consume_plane/plugins/trajectory_risk.py`, `persistence/query.py` | **HIGH** | The frontend re-implements the Bayesian noisy-OR probabilistic risk model in JavaScript instead of consuming expected loss from the backend (which is returned as `null`). If risk weights change on the backend, the frontend displays divergent scores. |
| **4** | **N+1 Polling Storm** | `static/js/riskmap.js` | `persistence/http_api/app.py`, `persistence/query.py` | **HIGH** | Every 30 seconds, `riskmap.js` fetches `/sessions?limit=50` and then fires `Promise.all` with **3 HTTP requests per session** (`/sessions/{id}`, `/trajectories/session/{id}`, and `/sessions/{id}/decisions`). For 50 sessions, this sends **151 concurrent requests** into SQLite, risking lock timeouts (`busy_timeout=1000ms`). |
| **5** | **Error Envelope Inconsistency** | `static/js/app.js`, `static/html/opencode-wrapper/index.html` | `persistence/http_api/app.py`, `web/sessions.py` | **MEDIUM** | `app.js` expects `{ error: { code, message, details } }`, while `web/sessions.py` throws standard FastAPI `HTTPException` returning `{ detail: "..." }`. `app.js` displays `HTTP 503` or undefined errors when hitting session/gateway errors. |
| **6** | **Health Check Split & False Status** | `static/js/app.js` (header), `static/js/metrics.js` | `web/main.py` (`/healthz`), `persistence/http_api/app.py` (`/api/v1/health`) | **MEDIUM** | The UI header checks `/healthz` on port 8000 and displays "Gateway online" even if `api:8790` (the evidence persistence API) is crashed or unreachable. |
| **7** | **Unauthenticated Policy Modification** | `static/js/config.js` | `configuration/api.py`, `Caddyfile` | **HIGH (Security)** | `PUT /api/v1/configs/{name}` and `PUT /api/v1/config-selection` have zero authentication, rate limiting, or CSRF protection. Any network client can overwrite security guardrails, allow arbitrary tools, or disable prompt injection rules. |
| **8** | **OpenCode Subprocess Lifecycle & Timeout** | `static/html/opencode-wrapper/index.html` | `web/sessions.py` | **MEDIUM** | If `OPENCODE_MODEL` is misconfigured or missing, `OpenCodeBackend` crashes on start with a 503 error. Frontend cleanup relies on `navigator.sendBeacon` or `pagehide` `fetch(DELETE, {keepalive})`, which can leave orphaned processes on unexpected tab kills. |
| **9** | **Audit Export Discrepancy** | `static/js/metrics.js` | `persistence/http_api/app.py` (`/api/v1/export/sessions/{id}`) | **LOW** | The general "Export audit log" button iterates through client-side paginated `/actions` (NDJSON) rather than using a streaming server-side export endpoint, risking browser memory crashes on large datasets. |
| **10** | **CORS Configuration Absence in Docker** | `compose.yaml` | `persistence/http_api/__main__.py` | **MEDIUM** | `compose.yaml` starts `persistence.http_api` without `--cors-origin`. While Caddy proxies everything on the same origin in production, direct local development against port 8790 fails CORS preflight. |

---

## 3. Deep Dive into Architectural & Functional Holes

### 3.1 The "Tests" Tab Backend Wiring

**Current Status:**
- The guardrail test suite execution in `static/js/tests.js` is wired to the backend: clicking "Run suite" triggers `POST /api/suite/runs` and polls `GET /api/suite/runs/latest`. In `web/suite.py`, a child process executes `pytest tests/control_layer` and reports real case results.
- The outcome check replays at the bottom of the tab (`Approved client, wrong name saved`, `Two agents onboard the same client`, `Agent opens someone else’s file`) remain canned client-side JavaScript scenarios (`SCEN`) rather than executing actual sandbox outcome replays.
- **Backend Reality:** The backend has:
  - Real verification rules in `contracts/verification.py`
  - A SQLite table `verification_results`
  - Endpoints `GET /api/v1/sessions/{session_id}/verification`
  - Unit/integration verification test suites in `tests/support/test_*.py`
- **Result:** Guardrail test suite is live; outcome replay cards remain synthetic simulations.

### 3.2 501 Not Implemented vs. Frontend Metrics Rendering

**Current Status:**
- In production (`compose.yaml`), `persistence.http_api` runs without `--example-mode`.
- The following endpoints intentionally return `501 Not Implemented`:
  - `GET /api/v1/metrics/security`
  - `GET /api/v1/metrics/usage`
  - `GET /api/v1/metrics/performance`
  - `GET /api/v1/detections` and `GET /api/v1/detections/{id}`
  - `GET /api/v1/catalog/detections`
- In `static/js/metrics.js`:
  ```javascript
  if (aggregates) {
    try { 
      served = await Promise.all(['/metrics/security', '/metrics/usage?group_by=agent', '/metrics/performance'].map(p => App.read(p))); 
    } catch (e) { 
      if (e.status === 501) aggregates = false; 
    }
  }
  ```
- When `aggregates` returns 501, `metrics.js` calls `derive(a.items)`:
  - `derive()` groups by `t.auditor` and counts `acted`.
  - To prevent broken tables, `metrics.js` calls `Mock.auditorRuns(auditor, rows)` from `static/js/mock.js`, which supplies deterministic stand-in metrics for `runs`, `p50`, and `p95` (with a simulated tooltip).
- **Result:** The Controls table renders simulated metrics rather than blank dashes, but true server-side aggregation across persistent stores is deferred.

### 3.3 Risk Model Inconsistency (Frontend Calculation vs. Backend Plugin)

**Problem:**
- In `consume_plane/plugins/trajectory_risk.py`, the trajectory risk plugin evaluates agent actions and records findings.
- The read API (`persistence/query.py`) returns:
  - `risk_level`: Highest severity among trajectory-risk findings (`low`, `medium`, `high`, `critical`).
  - `expected_loss`: `null` (not persisted in the findings projection).
  - `failure_probability`: `null`.
- In `static/js/riskmap.js` and `static/js/app.js`:
  - The frontend defines its own `Risk` object with hardcoded `weights`, `tools`, `sideEffect`, `prerequisites`, and `levels`.
  - When loading sessions (`loadSession`), it downloads every step of the trajectory and runs `Risk.signalsFor(...)` and `Risk.step(...)` inside the browser!
- **Consequences:**
  1. Any tuning or updates to backend weights (e.g., weights for `out_of_contract_tool`, `missing_prerequisite`) will cause immediate discrepancies between the backend's gateway interventions and the frontend's visual plot.
  2. If an action's arguments contain target IDs formatted slightly differently, the regex `/^[A-Z]{3}-\d{4}$/` in `app.js` can fail to recognize out-of-scope targets that the backend flagged.

### 3.4 Concurrency Bottleneck: N+1 Trajectory Polling Storm

**Problem:**
- In `static/js/riskmap.js`:
  ```javascript
  async function load() {
    const { items } = await get('/sessions?limit=50');
    App.sessions = await Promise.all(items.map(loadSession));
    // ...
  }
  setInterval(load, 30000);
  ```
- In `loadSession`:
  ```javascript
  const [detail, trajectory] = await Promise.all([
    get(`/sessions/${id}`),
    get(`/trajectories/session/${id}?view=full&kinds=tool_use&include_detections=false&limit=1000`)
  ]);
  ```
- **Impact:**
  - If 50 sessions exist, every 30 seconds the browser fires **1 + (50 * 2) = 101 HTTP requests** simultaneously.
  - In `persistence/query.py`, SQLite connections have `busy_timeout=1000` (1 second).
  - SQLite WAL mode allows concurrent readers, but opening 100 simultaneous SQLite connection threads in Python's `asyncio.to_thread` pool can exhaust thread pool workers, causing requests to stall or fail with `503 store_unavailable`.

### 3.5 Error Envelope Format Inconsistency

**Problem:**
- `persistence/http_api` and `configuration/api.py` return error payloads adhering to `docs/rest.md`:
  ```json
  {
    "error": {
      "code": "bad_request",
      "message": "malformed request parameter",
      "details": { "parameter": "name" }
    }
  }
  ```
- `web/sessions.py` (which handles the OpenCode chat session) uses default FastAPI `HTTPException`:
  ```json
  {
    "detail": "the agent is still working on the previous message"
  }
  ```
- In `static/js/app.js`:
  ```javascript
  async function request(url, opts) {
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const fields = body.error?.details?.fields;
      throw Object.assign(new Error((body.error?.message || `HTTP ${r.status}`) + (fields ? `: ${fields.join(', ')}` : '')), { status: r.status });
    }
    return body;
  }
  ```
- When `web/sessions.py` returns an error, `body.error` is `undefined`. Any shared caller in `app.js` will format this as a generic `HTTP <status>` instead of showing the actual server failure message.

### 3.6 Routing & Split-Brain Configuration Management

**Problem:**
- In `Caddyfile`:
  ```caddy
  @readapi {
      path /api/v1 /api/v1/*
      not path /api/v1/configs /api/v1/configs/* /api/v1/config-selection
  }
  handle @readapi {
      reverse_proxy api:8790
  }
  handle {
      reverse_proxy app:8000
  }
  ```
- In `compose.yaml`:
  - `app` has `CONFIG_DIR: /data/config`.
  - `api` does **not** have `CONFIG_DIR` specified; it uses default fallback directories.
- Both `web.main:app` and `persistence.http_api` call `install_config_api(app)`.
- **Result:**
  - If a client accesses `api:8790` directly (e.g., during backend testing or via an internal tool), configuration changes are written to the default local path, while requests via Caddy write to `/data/config`.
  - Both processes run independent in-memory configuration caches with no cross-process file-watching or sync events.

---

## 4. What Needs to Be Fixed (Remediation Plan)

### Priority 0: Immediate Fixes (Hackathon Demo & Stability)

1. **Fix `tests.js` Mocking:**
   - Connect the outcome replays in `static/js/tests.js` to real data:
     - Query `GET /api/v1/sessions` and `GET /api/v1/sessions/{id}/verification`.
     - Render actual check statuses (`VERIFIED_SUCCESS`, `FAILED_POSTCONDITIONS`, `VERIFICATION_INCOMPLETE`).
   - If test suite runs are desired on demand, add a backend endpoint `POST /api/v1/tests/run` that executes a synthetic verification run and streams or returns the result.

2. **Fix `derive()` Metrics Fallback in `metrics.js`:**
   - In `metrics.js`, provide sensible estimates for `runs`, `p50`, and `p95` in `derive()` when `perf` is calculated from raw action rows:
     - Estimate `runs` as total actions evaluated for deterministic controls.
     - Provide fallback values instead of `undefined` to prevent broken table columns.
   - Guard against `a.decision == null` so `by[a.decision]++` does not produce `NaN`.

3. **Batch Session & Trajectory Loading in `riskmap.js`:**
   - Replace the `Promise.all` fanout of 100+ requests with:
     - A limit on active sessions to load (e.g., latest 10–15 sessions instead of 50).
     - Or a dedicated endpoint / query parameter that returns session summaries with step counts and worst-step impact pre-aggregated, avoiding full trajectory downloads for every session.

4. **Standardize Error Handling in `web/sessions.py`:**
   - Update `web/sessions.py` to return the standardized error envelope:
     ```python
     return JSONResponse(status_code=status, content={"error": {"code": code, "message": detail}})
     ```
   - Update `opencode-wrapper/index.html` and `app.js` to handle both `body.error.message` and `body.detail`.

### Priority 1: Architectural & Contract Alignment

5. **Expose Expected Loss & Probabilities in Backend:**
   - Update `persistence/query.py` session projections to calculate or extract `expected_loss`, `P`, and `maxC` from stored `consumer_findings` where `plugin == 'trajectory-risk'`.
   - Update `riskmap.js` to read these values directly from the session record when available, using the client-side `Risk` engine only as a fallback.

6. **Unified Health Reporting:**
   - Update the UI header health check in `app.js` to probe both `/healthz` (web/gateway) and `/api/v1/health` (persistence read store).
   - Display a degraded badge if the persistence layer is down while the web server is up.

7. **Ensure Consistent Environment in `compose.yaml`:**
   - Add `CONFIG_DIR: /data/config` to the `api` service in `compose.yaml` to ensure both containers operate on identical configuration directories if touched directly.
   - Pass `--cors-origin` to `persistence.http_api` in `compose.yaml` so developers testing frontends outside of Docker (e.g. Vite on `:5173`) are not blocked by CORS errors.

### Priority 2: Security & Production Hardening

8. **Secure Configuration Mutators:**
   - Add basic token authentication or a shared admin secret header (`X-Admin-Token` or Bearer) to `PUT /api/v1/configs/{name}` and `PUT /api/v1/config-selection`.
   - Add CSRF protection or SameSite cookie verification if invoked from browser sessions.

9. **Process & Resource Caps for OpenCode Wrapper:**
   - In `web/sessions.py`, verify that idle sessions are killed reliably.
   - Enforce process group termination with `preexec_fn=os.setsid` on subprocess creation to prevent zombie processes if the parent process terminates abruptly.
