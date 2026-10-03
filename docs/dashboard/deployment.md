# Deployment, Domain and LLM Hosting

Status: proposed, not implemented. Free-tier limits below are from memory; verify them on signup.

Goal: judges open one public HTTPS URL (or scan the QR code) and everything works. No team laptops,
no venue Wi-Fi, no cold starts during judging.

---

## Rules check

Criteria PDF §7: teams are expected to use open-source libraries and **local models (such as those run
via Ollama)**, and must be able to **run the entire system on their own setup**. No paid subscriptions (OpenAI, Anthropic, Copilot, ...) are provided.

Our reading:
- An open-weight model that **we deploy on a cloud machine we control** is "our own setup". Allowed.
- Paid LLM APIs: not allowed.
- Free third-party hosted LLM APIs (Groq, Gemini free tier, ...): grey area. Not used for anything judged.
- The judge-run test suite must work **without any LLM**. The real LLM is used only for the live demo.

---

## Stack

| Part | Choice | Why |
|---|---|---|
| Gateway + API | FastAPI + uvicorn | Python like the rest of the repo; serves the API and the dashboard from one process and one origin (no CORS) |
| Dashboard | Plain HTML/CSS/JS in `/static`, live feed over SSE (`EventSource`) | No build step; meets the "no CDN, everything bundled" rule from [dashboard-ui.md](dashboard-ui.md); the custom audit-ledger look is easy without a framework |
| Semantic guard | Small prompt-injection classifier, loaded in-process (e.g. `protectai/deberta-v3-base-prompt-injection-v2`, Apache-2.0) | ~1 GB RAM, tens of ms on CPU. Fast enough for every request |
| LLM (the agent we protect) | `llama-server` (llama.cpp), OpenAI-compatible API, **Qwen2.5-7B-Instruct** GGUF Q4_K_M (~4.7 GB, Apache-2.0), on a GPU | Good tool calling for its size. One binary, the same GGUF works on GPU (CUDA image) and CPU (fallback). The gateway talks to it through one base URL in the policy YAML, so it can be swapped |
| Packaging | One Docker image (or `docker compose` with gateway + llama-server) | Same artifact locally and in the cloud |

Rejected: Streamlit (reruns the whole script on each interaction, hard to share a live feed between
judges, can't do the custom look), React (adds a build step with no real benefit for 4 views),
Ollama on laptops (laptops sleep, venue Wi-Fi isolates clients, slow).

### Classifier vs LLM
- The **classifier is a security control**, so it runs on every request and its latency shows in the pipeline trace.
- The **LLM is the system being protected**: it answers the chat console and drives the agent demo.
  It never runs as a blocking security check.
- 7B speed estimates (verify): GPU (L4/A10G) ~30–60 tokens/s; Oracle 4-core ARM CPU ~2–4 tokens/s; HF free 2 vCPU ~1–2 tokens/s.
  CPU is too slow for live judging (40–90 s per answer), so **7B runs on a GPU**. Always stream responses and cap `max_tokens`.
- Download model weights into the image **at build time**, not at startup.

---

## Server options (free)

| Option | Resources | Fits | Downsides |
|---|---|---|---|
The app and the LLM are hosted separately: the app runs on a free CPU box, and the 7B LLM on a GPU.

| Option | Resources | Role | Downsides |
|---|---|---|---|
| **A. Oracle Cloud Always Free, ARM VM** (preferred app host) | up to 4 OCPU / 24 GB RAM, persistent disk | Gateway + dashboard + classifier. Also the **CPU fallback** for the 7B LLM (slow, but it works if Modal is down) | Card needed for verification; ARM capacity is sometimes unavailable; we set up Docker + HTTPS ourselves. **Sign up on day 1.** |
| **B. Hugging Face Spaces, Docker SDK** (fallback app host) | ~2 vCPU / 16 GB RAM, free CPU tier | Gateway + dashboard + classifier. Too slow for 7B | Disk resets on restart (seed demo data at startup); sleeps after ~48 h without traffic; deploy by `git push` |
| **C. Modal (serverless GPU, free monthly credits)** (LLM host) | L4 / A10G GPU (24 GB) | `llama-server` CUDA image + Qwen2.5-7B Q4, exposed as an HTTPS web endpoint | Cold start 30–60 s. Keep 1 container warm (`min_containers=1`) **only during judging**; credits (~$30/month, verify) cover roughly 30 GPU-hours. Endpoint protected by a token |

Not used: Render / Railway / Koyeb free tiers (≤512 MB RAM, cold starts), AWS ECS (Fargate is not free;
the free EC2 micro has 1 GB RAM), Colab / Kaggle (not meant for serving).

### Option A setup sketch (Oracle)
1. Create an Ampere A1 VM (Ubuntu, 4 OCPU / 24 GB). Open ports 80/443 in the VCN security list **and** in the VM firewall (`iptables`).
2. Install Docker; `git clone` the repo; `docker compose up -d`.
3. HTTPS: Caddy in front (automatic Let's Encrypt cert for our domain), or a Cloudflare Tunnel (no open ports needed).
4. `restart: unless-stopped` on all services so a reboot doesn't take the demo down.

### Option B setup sketch (HF Spaces)
1. New Space → SDK: Docker. The `Dockerfile` must listen on port `7860` (or set `app_port` in the Space README header).
2. Push the repo (or a deploy branch) to the Space git remote.
3. Set `llm.base_url` to the Modal endpoint, and store the Modal token as a Space secret.

### Option C setup sketch (Modal, LLM)
1. One `modal_llm.py`: a Modal image from `ghcr.io/ggml-org/llama.cpp:server-cuda`, with the Qwen2.5-7B-Instruct Q4_K_M GGUF stored in a Modal Volume (downloaded once, not on every start).
2. Run `llama-server -m <gguf> -ngl 99 --parallel 4 --ctx-size 16384 --api-key $LLM_TOKEN` as a `@modal.web_server` on an L4.
   `--parallel 4` lets several judges chat at once.
3. `modal deploy modal_llm.py` gives a fixed HTTPS URL, which goes into `llm.base_url`.
4. Before judging, set `min_containers=1` and redeploy. After judging, set it back to 0 so we don't burn credits.

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
  base_url: https://<workspace>--llm-serve.modal.run/v1   # fallback: http://llama-server:8080/v1 (7B on Oracle CPU)
  model: qwen2.5-7b-instruct-q4_k_m
  api_key_env: LLM_TOKEN
  max_tokens: 400
  stream: true
  timeout_s: 60
semantic_guard:
  model: protectai/deberta-v3-base-prompt-injection-v2
  block_threshold: 0.9
```

Admin actions (policy mode changes, data reset) need a token from an env var / Space secret, never committed.
Public judge sessions stay sandboxed (see [dashboard-ui.md](dashboard-ui.md#practical-constraints)).

---

## Before judging

- [ ] Public URL loads on a phone over mobile data (not venue Wi-Fi).
- [ ] Hit the URL once to warm it up (HF Space awake, classifier loaded).
- [ ] Modal `min_containers=1` deployed; the first chat answer comes back in a few seconds; check the remaining credits.
- [ ] Fallback tested: change `llm.base_url` to the Oracle CPU `llama-server` and confirm the policy hot-reload picks it up.
- [ ] Demo data seeded; the reset button works.
- [ ] Test suite runs from a fresh clone with **no LLM available** (uses a stub).
- [ ] If the LLM is down, the chat console shows a clear "LLM offline" state; guard checks still run.
- [ ] Recorded trajectory replay works without the LLM.

---

## Open questions

1. Who owns the Oracle signup, and does it succeed (ARM capacity)? If not by the evening of day 1, commit to HF Spaces.
2. ~~Model size~~: decided, **Qwen2.5-7B-Instruct on a Modal GPU**. Still open: does someone have a Modal account with credits, and is our tool-calling prompt format reliable with 7B?
3. Real domain or the `hf.space` URL? It only matters for the QR code and the slides.
