# Deployment, Domain and LLM

Status: proposed, not implemented. Hosting free-tier limits below are from memory; verify them on signup.

Goal: judges open one public HTTPS URL (or scan the QR code) and everything works. No team laptops,
no venue Wi-Fi, no cold starts during judging.

---

## LLM decision

We use **paid LLM APIs** (team decision). Keys come from env vars / host secrets, never committed.
- The **agent we protect** and the **semantic guard** both call hosted API models.
- API spend is itself governed: the budget controls in the policy YAML cap tokens and cost per session and per day.
- The judge-run test suite must work **without any API key**. It uses a stub; the real API is used only for the live demo.

---

## Stack

| Part | Choice | Why |
|---|---|---|
| Gateway + API | FastAPI + uvicorn | Python like the rest of the repo; serves the API and the dashboard from one process and one origin (no CORS) |
| Dashboard | Plain HTML/CSS/JS in `/static`, live feed over SSE (`EventSource`) | No build step; meets the "no CDN, everything bundled" rule from [dashboard-ui.md](dashboard-ui.md); the custom audit-ledger look is easy without a framework |
| Semantic guard | Fast, cheap API model (e.g. Claude Haiku 4.5) as a prompt-injection judge, returning a score | No model weights to host; latency shows in the pipeline trace |
| LLM (the agent we protect) | Stronger API model (e.g. Claude Sonnet 5.5) with tool calling | Reliable tool calling; the gateway talks to it through one provider/model setting in the policy YAML, so it can be swapped |
| Packaging | One Docker image | Same artifact locally and in the cloud |

Rejected: Streamlit (reruns the whole script on each interaction, hard to share a live feed between
judges, can't do the custom look), React (adds a build step with no real benefit for 4 views),
running on team laptops (laptops sleep, venue Wi-Fi isolates clients).

### Guard vs agent
- The **semantic guard is a security control**, so it runs on every request that reaches it (after the
  deterministic checks) and its latency shows in the pipeline trace. A hard deny skips it and saves the API call.
- The **agent LLM is the system being protected**: it answers the chat console and drives the agent demo.
  It never runs as a blocking security check.
- Always stream agent responses and cap `max_tokens`. Set request timeouts; on provider errors the guard fails closed.

---

## App hosting (free)

The app (gateway + dashboard) is a light CPU process; the models are behind the API.

| Option | Resources | Role | Downsides |
|---|---|---|---|
| **A. Oracle Cloud Always Free, ARM VM** (preferred) | up to 4 OCPU / 24 GB RAM, persistent disk | Gateway + dashboard | Card needed for verification; ARM capacity is sometimes unavailable; we set up Docker + HTTPS ourselves. **Sign up on day 1.** |
| **B. Hugging Face Spaces, Docker SDK** (fallback) | ~2 vCPU / 16 GB RAM, free CPU tier | Gateway + dashboard | Disk resets on restart (seed demo data at startup); sleeps after ~48 h without traffic; deploy by `git push` |

Not used: Render / Railway / Koyeb free tiers (≤512 MB RAM, cold starts), AWS ECS (Fargate is not free;
the free EC2 micro has 1 GB RAM).

### Option A setup sketch (Oracle)
1. Create an Ampere A1 VM (Ubuntu, 4 OCPU / 24 GB). Open ports 80/443 in the VCN security list **and** in the VM firewall (`iptables`).
2. Install Docker; `git clone` the repo; `docker compose up -d`.
3. HTTPS: Caddy in front (automatic Let's Encrypt cert for our domain), or a Cloudflare Tunnel (no open ports needed).
4. `restart: unless-stopped` on all services so a reboot doesn't take the demo down.

### Option B setup sketch (HF Spaces)
1. New Space → SDK: Docker. The `Dockerfile` must listen on port `7860` (or set `app_port` in the Space README header).
2. Push the repo (or a deploy branch) to the Space git remote.
3. Store the LLM API key as a Space secret.

---

## Domain

| Option | Example | Cost |
|---|---|---|
| Platform subdomain | `https://<team>-ai-control-layer.hf.space` | Free, zero setup |
| Free subdomain → VM IP | `aiguard.duckdns.org`, `<ip>.sslip.io` | Free |
| Real domain (GitHub Student Pack: `.me` via Namecheap or `.tech`) | `guard.<team>.tech` | Free for 1 year |

With a real domain: point its nameservers to **Cloudflare (free plan)**, then either
- Option A: an `A` record to the Oracle VM IP (or a named Cloudflare Tunnel), or
- Option B: a redirect rule to the `hf.space` URL (as far as we know, a custom domain directly on a Space needs a paid HF plan).

The QR code in the dashboard header points to the final URL. Generate it **after** the domain is settled.

---

## Configuration

All environment-specific values come from one place (policy YAML / env vars), so the same image runs locally and in the cloud:

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-5-5
  api_key_env: LLM_API_KEY
  max_tokens: 400
  stream: true
  timeout_s: 60
semantic_guard:
  provider: anthropic
  model: claude-haiku-4-5
  api_key_env: LLM_API_KEY
  block_threshold: 0.9
  on_error: block        # fail closed
```

Admin actions (policy mode changes, data reset) need a token from an env var / Space secret, never committed.
Public judge sessions stay sandboxed (see [dashboard-ui.md](dashboard-ui.md#practical-constraints)).

---

## Before judging

- [ ] Public URL loads on a phone over mobile data (not venue Wi-Fi).
- [ ] Hit the URL once to warm it up (HF Space awake).
- [ ] API key set as a secret; the first chat answer comes back in a few seconds; check the account spend limit.
- [ ] Model swap tested: change `llm.model` in the policy YAML and confirm the hot-reload picks it up.
- [ ] Demo data seeded; the reset button works.
- [ ] Test suite runs from a fresh clone with **no API key** (uses a stub).
- [ ] If the LLM is down, the chat console shows a clear "LLM offline" state; guard checks still run.
- [ ] Recorded trajectory replay works without the LLM.

---

## Open questions

1. Who owns the Oracle signup, and does it succeed (ARM capacity)? If not by the evening of day 1, commit to HF Spaces.
2. Who owns the API account and key, and what spend limit do we set on it?
3. Real domain or the `hf.space` URL? It only matters for the QR code and the slides.
