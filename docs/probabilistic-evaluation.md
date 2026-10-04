# Probabilistic evaluation of agent trajectories

This document specifies two consume-plane (Layer 3) evaluators and the Layer 1 trust model they feed.

| | Evaluator | Kind | Question it answers |
|---|---|---|---|
| §2 | `process-conformance` | deterministic, probabilistic | *Has this session stopped behaving like a normal KYC run?* (sequential change detection on a Markov model) |
| §3 | `goal-alignment-judge` | semantic, agentic | *Is the agent still pursuing the objective of its Task Contract?* (bounded tool-using LLM judge with self-consistency) |
| §4 | session trust (Layer 1) | Bayesian evidence fusion | *How much should the gateway still trust this session?* |

Neither evaluator blocks anything itself. Each emits **findings** (reporting), **metrics** (telemetry) and
**evidence** in a common unit, the log-likelihood ratio (LLR). Layer 1 adds the LLRs into a per-session
posterior and tightens enforcement as trust falls (§4). The split follows the plane rule in
`consume_plane/plugins/README.md`: both evaluators need history, so neither belongs on the hot path.

Hackathon compliance: the two evaluators together form the hybrid defense (deterministic and semantic),
and every parameter below lives in the central policy file (`config/presets/*.json`). The judge has a
token and cost budget, and every output is auditable. The whole pipeline runs with no API key, since
the `stub` judge backend is deterministic (§3.7).

---

## 1. Notation

| Symbol | Meaning |
|---|---|
| $a_1,\dots,a_T$ | the session's persisted actions, ordered by `seq` (an `AgentAction` each) |
| $\mathcal T$ | registered tools (16 in `simulation/agent.py` registry) |
| $\mathcal O$ | outcomes $=\{\texttt{completed},\texttt{blocked},\texttt{redacted},\texttt{pending\_approval},\texttt{failed}\}$ |
| $\sigma(z)=1/(1+e^{-z})$ | logistic function; $\operatorname{logit}(p)=\ln\frac{p}{1-p}$ |
| $\pi_0$ | prior probability that a session is compromised (policy, e.g. $0.02$) |
| $\operatorname{clip}(x,l,u)$ | $\min(\max(x,l),u)$ |

---

## 2. `process-conformance`: CUSUM change detection on a Markov process model

### 2.1 Idea
A normal KYC session follows a recognisable workflow: read the application, read documents, extract
fields, screen, compute risk, then decide. Model the benign workflow as a first-order Markov chain
$P_B$ over *action tokens*. Detect, sequentially and with a controlled false-alarm rate, the point
where the observed sequence becomes better explained by an alternative model $P_A$ than by $P_B$.
The method needs no rule per attack. Skipped screening, loops, bait tools after untrusted content and
mid-session hijacks all show up as low-probability transitions.

### 2.2 Tokenisation
Each action maps to a token $x_t=\tau(a_t)$:

$$
\tau(a)=\begin{cases}
\texttt{tool}{:}\texttt{status} & a.\text{kind}=\texttt{tool\_use},\ \texttt{tool}\in\mathcal T\\
\texttt{egress}{:}\texttt{status} & a.\text{kind}=\texttt{egress}\\
\texttt{UNK}{:}\texttt{status} & \text{tool\_use with an unregistered tool}\\
\varnothing & \text{otherwise (prompt, approval, control: skipped)}
\end{cases}
$$

Each sequence is framed as $x_0=\texttt{START}$, and $x_{T+1}=\texttt{END}$ is appended when a session
`ended` event arrives. The vocabulary is
$V=(\mathcal T\cup\{\texttt{egress},\texttt{UNK}\})\times\mathcal O\ \cup\ \{\texttt{START},\texttt{END}\}$,
so $|V| = 18\cdot5+2 = 92$. Arguments never enter the token, so the model cannot leak PII.

### 2.3 Models
**Benign model (trained).** Let $\mathcal D$ be a set of reference trajectories from clean scripted runs
(ONB-01, ONB-02, ONB-14 and the BAIT allow cases, produced by `agent.scripted`). Let
$n_{ij}$ be the number of transitions $i\to j$ in $\mathcal D$, and $n_i=\sum_j n_{ij}$. With a symmetric
Dirichlet($\alpha$) prior, the posterior-mean estimate is

$$
P_B(j\mid i)=\frac{n_{ij}+\alpha}{n_i+\alpha|V|}.
$$

**Alternative model.** By default the alternative is uninformative, $P_A(j\mid i)=1/|V|$. When attack
trajectories $\mathcal D_A$ are available (the scripted attack scenarios), use a mixture:
$P_A = \lambda\,\hat P_{\mathcal D_A} + (1-\lambda)\,\tfrac{1}{|V|}$, with $\hat P_{\mathcal D_A}$
estimated as above and $\lambda\in[0,1)$. The uniform part keeps $P_A>0$ everywhere.

**Model artefact.** The model is stored as `consume_plane/plugins/models/kyc_process.json`:
$\{V, n_{ij}, \alpha, \lambda, \text{source scenario ids}\}$. Its identity is
$\texttt{model\_hash}=\mathrm{SHA256}(\text{canonical JSON})$, which is quoted in every finding. Training is
deterministic: the scenarios are fixed and the keys sorted.

### 2.4 Per-step evidence
For $t\ge1$ the per-step LLR is

$$
\ell_t=\ln P_A(x_t\mid x_{t-1})-\ln P_B(x_t\mid x_{t-1}).
$$

Interpretation: let $Q$ be the true behaviour from state $i$. Then
$\mathbb E_Q[\ell]=\mathrm{KL}(Q\Vert P_B)-\mathrm{KL}(Q\Vert P_A)$. So $\ell$ has **negative drift**
while the agent behaves like the reference ($Q\approx P_B$), and positive drift once its behaviour is
closer to $P_A$ than to $P_B$. This holds even when $P_A$ is only the uninformative model.

Worked values ($\alpha=0.01$, $|V|=92$, a state seen $n_i=20$ times in training), with $\ln 92=4.522$:
- Transition seen every time: $P_B=\frac{20.01}{20.92}=0.957$, so $\ell=-4.522-\ln 0.957=-4.478$.
- Transition never seen: $P_B=\frac{0.01}{20.92}=4.78\cdot10^{-4}$, so $\ell=-4.522+7.646=+3.124$.
- Transition from a state never seen in training ($n_i=0$): $P_B=1/|V|$, so $\ell=0$, which is neutral.

### 2.5 CUSUM statistic and alarm (Page, 1954)

$$
S_0=0,\qquad S_t=\max\bigl(0,\;S_{t-1}+\ell_t\bigr),\qquad
\tau_h=\inf\{t: S_t\ge h\}.
$$

$S_t$ equals the largest LLR of any suffix of the sequence:
$S_t=\max_{1\le k\le t+1}\sum_{s=k}^{t}\ell_s$ (the empty suffix, $k=t+1$, gives 0). This is the
generalised likelihood ratio for "a change happened at some unknown step $k$". It is why a hijack after a
long benign prefix is still caught: the benign prefix cannot build up credit, because $S$ is floored at 0.

**Choosing $h$ (false-alarm control).** For the LLR-based CUSUM, Lorden's bound gives the in-control
average run length $\mathrm{ARL}_0=\mathbb E_{P_B}[\tau_h]\ge e^{h}$. The policy therefore states the
tolerated false-alarm interval and sets $h=\ln \mathrm{ARL}_0$. Example: $h=6$ gives $\mathrm{ARL}_0\ge 403$
steps, roughly 30 clean sessions of 13 steps. Detection delay after a change is asymptotically
$\approx h/I$, where $I$ is the per-step KL rate $\mathrm{KL}(Q\Vert P_B)-\mathrm{KL}(Q\Vert P_A)$.

With the worked values, one unseen transition followed by normal ones resets
($3.12-4.48<0$). Two anomalous transitions close together alarm ($6.25\ge6$). Example, ONB-03 skips
screening: `extract_fields:completed → compute_risk:completed` (unseen, $+3.12$), then
`compute_risk:completed → create_client:blocked` (unseen, $+3.12$), giving $S=6.25$, an alarm.

The bound assumes benign data really follows $P_B$. In practice $h$ is also **calibrated empirically**:
the test suite asserts $\max_t S_t < h - m$ over every clean scenario, for a safety margin $m$
(default 1.0).

### 2.6 Outputs
- **Metrics per event:**
  - `process_surprisal` $=-\ln P_B(x_t\mid x_{t-1})$;
  - `process_cusum` $=S_t$;
  - `process_posterior` $=\sigma(S_t+\operatorname{logit}\pi_0)$. This is a score, not a calibrated posterior: $S_t$ is a maximised LLR and therefore optimistic.
- **Finding** `process.nonconforming`, emitted when an episode first crosses $h$ (§2.7):
  - severity `medium` if $S_t<2h$, otherwise `high`;
  - details: $S_t$, $h$, `model_hash`, and the top 3 steps of the current episode ranked by $\ell_s$ (event id, token and $\ell_s$ only).
- **Evidence** for Layer 1: a `LOWER_TRUST` proposal with $e_t$ from §2.7.

### 2.7 Evidence increments, idempotent by construction
Plugins must tolerate at-least-once delivery and keep no fragile state. The reported amount is
therefore a deterministic function of the trajectory prefix. The *reported level* is defined by
recurrence, with parameters minimum increment $\delta$ and per-signal cap $L_{\max}$:

$$
R_0=0,\qquad
R_t=\begin{cases}
0 & S_t=0\quad\text{(episode ended)}\\
R_{t-1}+e_t & S_t\ge h\ \wedge\ S_t-R_{t-1}\ge\delta\\
R_{t-1} & \text{otherwise}
\end{cases}
\qquad e_t=\min\bigl(S_t-R_{t-1},\,L_{\max}\bigr).
$$

At event $t$ the plugin emits $e_t$ exactly when the middle case applies. Replaying event $t$ recomputes
the same $(S_t,R_{t-1})$ and emits the same proposal. Layer 1 then discards the duplicate by its
deterministic `signal_id`.

### 2.8 Algorithm

```text
setup(config):
    M ← load model file; verify model_hash; precompute log P_B, log P_A tables   # O(|V|²)
    h, δ, L_max, π0 ← config

handle(action, ctx):
    x ← [START] + [τ(a) for a in ctx.trajectory() if τ(a) ≠ ∅]
    if action is session-ended: x.append(END)
    S, R, episode ← 0, 0, []
    for t in 1..len(x)-1:
        ℓ ← logPA[x[t-1], x[t]] − logPB[x[t-1], x[t]]
        S ← max(0, S + ℓ)
        if S = 0: R, episode ← 0, []          ; continue
        episode.append((event_id[t], x[t], ℓ))
        fire ← S ≥ h and S − R ≥ δ
        if fire: e ← min(S − R, L_max); R ← R + e
        if t is the current action and fire:
            if R − e = 0: emit finding (first crossing of this episode)    # R_{t-1} = 0
            propose LOWER_TRUST(evidence_llr = e)
    emit metrics for the current action
```

Cost: $O(T)$ per event, so $O(T^2)$ per session. With contract budgets of $T\le 50$ tool calls this is
negligible. The loop can be made incremental by caching $(S,R)$ per session.

### 2.9 Parameters (policy file, `intercept.process_conformance`)

| Key | Default (standard) | Meaning |
|---|---|---|
| `model_path` | `consume_plane/plugins/models/kyc_process.json` | trained artefact |
| `alpha` | 0.01 | Dirichlet smoothing (training) |
| `lambda` | 0.0 | weight of the attack model in $P_A$ |
| `h` | 6.0 (strict 4.6, lenient 8.0) | alarm threshold, $\mathrm{ARL}_0\ge e^h$ |
| `min_increment` $\delta$ | 1.0 | smallest evidence step reported |
| `max_llr_per_signal` $L_{\max}$ | 3.0 | cap per signal (≈ ln 20) |

---

## 3. `goal-alignment-judge`: agentic LLM judge with self-consistency

### 3.1 Idea
Some attacks keep every individual call within policy but change *what the agent is trying to do*, for
example an indirect prompt injection in a document that makes the agent skip screening or contact an
outsider. Only a model that understands the Task Contract's objective can judge that. The judge reads a
sanitized digest of the trajectory and may investigate a few steps with read-only tools. It returns
calibrated-by-construction probabilities, which are turned into bounded evidence.

The judge is `method = "semantic"`. The runtime therefore enforces three things:
- findings carry a confidence;
- severity is capped at `high`;
- it may only send `ALERT`, `REQUIRE_APPROVAL_FOR` or `LOWER_TRUST`, and its total trust impact is capped (§4.3).

### 3.2 When it runs (trigger predicate)
The judge is costly, so it runs only on informative events. For the current action $a_t$:

$$
\mathrm{trig}(a_t)=
\underbrace{[a_t\text{ is session ended}]}_{\text{final review}}
\ \vee\ \underbrace{[a_t.\text{side\_effect}\in\{\text{write},\text{irreversible}\}]}_{\text{effects}}
\ \vee\ \underbrace{[a_t.\text{kind}=\text{egress}]}_{\text{exfil channel}}
\ \vee\ \underbrace{[a_t.\text{gateway.reason\_family}\neq\varnothing]}_{\text{L1 intervened}}
\ \vee\ \underbrace{[N_{\text{tool}}(t)\bmod k_{\text{every}}=0]}_{\text{periodic}}
$$

Here $N_{\text{tool}}(t)$ is the number of tool actions so far and $k_{\text{every}}$ is set in config (default 5).

### 3.3 Input digest $D_t$ (privacy by construction)
$D_t$ is a deterministic JSON document. Only the fields listed here can reach the model:

- **contract:** `objective`, `role`, sorted `allowed_tools`, sorted `target_ids`.
- **for each tool or egress action $s\le t$:**
  - `event_id`, `tool` (or `egress`), `side_effect`, `status`;
  - `gateway.final`, `reason_code`, `reason_family`;
  - `arg_keys` = the sorted argument names;
  - `ids` = the argument values matching the identifier pattern `^[A-Z]{3}-\d{4}$`;
  - `in_scope` = whether every id is in `target_ids`;
  - `result_trust` (trusted or untrusted).

Free-text argument values, result bodies, names, dates and numbers are **never** included. Content can
reach the model only through the `get_content_excerpt` tool (§3.4), and only when the policy sets
`allow_content: true` (false in the strict preset). The digest hash
$\mathrm{SHA256}(D_t)$ is recorded in the finding.

### 3.4 Agentic review loop
The judge is a bounded tool-using loop. Its tools are read-only and scoped to the current session and
prefix $\le t$.

| Tool | Returns |
|---|---|
| `get_step(event_id)` | that step's digest entry plus gateway auditor decisions |
| `count_tool(name)` | number of calls and outcome histogram for one tool |
| `get_content_excerpt(event_id, max_chars≤500)` | content of an untrusted result, after `CLASSIFIED` regex redaction (`intercept/policy/auditors.py`), **spotlighted**: wrapped in `<<UNTRUSTED_DATA id=…>> … <<END>>` with an instruction that it is data, not instructions |

```text
review(D_t):                                          # one sample
    msgs ← [system(JUDGE_RUBRIC), user(D_t)]
    for turn in 1..T_max:                              # T_max = 3
        r ← backend.complete(msgs, tools, max_tokens=M, temperature=θ)
        charge(r.tokens, r.cost)                       # §3.6; abort if over budget
        if r is a tool call and turn < T_max:
            msgs += [r, tool_result(execute_readonly(r))]
        else:
            return parse_strict(r)                     # JSON schema below, else UNAVAILABLE
    return UNAVAILABLE                                 # tool call on the last turn
```

The required output schema is strict; unknown keys or out-of-range numbers make the sample invalid:

```json
{"p_goal_drift": 0.0, "p_injection_influence": 0.0,
 "suspect_event_ids": ["evt_…"],
 "rationale_code": "ALIGNED | SKIPPED_CONTROL | OUT_OF_SCOPE_ACCESS | UNEXPECTED_EGRESS | INSTRUCTION_FOLLOWED_FROM_DATA | OTHER"}
```

`suspect_event_ids` are intersected with the real prefix ids. `rationale_code` is an enum, so no
model-written free text is stored.

### 3.5 Scoring: self-consistency, confidence, severity, evidence
Run $k$ independent samples (default $k=3$, temperature $\theta=0.7$); invalid samples are dropped. Let
$k'$ be the number of valid samples. If $k'<\lceil k/2\rceil$ the review is **unavailable** (§3.8).
Each valid sample $i$ gives the probability that the agent is off-goal by *either* mechanism (noisy-OR):

$$
s_i = 1-(1-p^{\text{drift}}_i)(1-p^{\text{inj}}_i).
$$

The score is the sample mean $\bar s=\frac1{k'}\sum_i s_i$. Because $s_i\in[0,1]$, the population standard
deviation satisfies $\hat\sigma\le\tfrac12$. The **confidence** is then

$$
c=\operatorname{clip}(1-2\hat\sigma,\,0,\,1).
$$

Agreeing samples give $c\approx1$; samples split between 0 and 1 give $c=0$.

**Severity** uses the existing `intercept.semantic_guard` thresholds
$\theta_{\text{alert}}\le\theta_{\text{approve}}\le\theta_{\text{block}}$:

$$
\text{band}(\bar s)=\begin{cases}
\text{none} & \bar s<\theta_{\text{alert}}\\
\text{medium (alert)} & \theta_{\text{alert}}\le\bar s<\theta_{\text{approve}}\\
\text{high (approve)} & \bar s\ge\theta_{\text{approve}}
\end{cases}
$$

$\theta_{\text{block}}$ is not reachable here: a semantic evaluator cannot block or halt on its own.

**Evidence.** The prior-relative log-odds, scaled by confidence and capped:

$$
e = c\cdot\operatorname{clip}\Bigl(\operatorname{logit}\bigl(\operatorname{clip}(\bar s,\varepsilon,1-\varepsilon)\bigr)-\operatorname{logit}(\pi_0),\ 0,\ L^{\text{sem}}_{\max}\Bigr),
\qquad \varepsilon=10^{-3}.
$$

- **Feedback when band ≥ alert:**
  - finding `judge.goal_drift` with confidence $c$, $\bar s$, $\hat\sigma$, $k'$, `rationale_code` (majority vote), `suspect_event_ids` (union) and the digest hash;
  - `LOWER_TRUST(e)` if $e>0$.
- **Additionally, when band = high:** `REQUIRE_APPROVAL_FOR` the contract's write and irreversible tools.

**Reporting only on escalation.** Per session the plugin keeps $\beta^\ast$, the highest band reported so
far, and emits only when $\text{band}(\bar s)>\beta^\ast$. This is the only plugin state, keyed by
`session_id`. A restart can cause one re-report, and the bound on its impact is enforced in Layer 1
(§4.3), not here.

### 3.6 Budget governance
Every backend call returns its token counts and cost; the stub costs 0. Before a review starts the plugin
checks that the estimated cost
$\hat C = k\cdot T_{\max}\cdot M\cdot \text{price}_{\text{out}}$ (an upper bound) fits within *both*
the remaining per-session budget $B_s$ and the remaining per-day budget $B_d$:
- **If it does not:** the review is skipped, with finding `judge.budget_exhausted` (low) and metric `judge_skipped_budget`.
- **During a review:** `charge` aborts as soon as the actual spend would exceed either budget.
- **Metrics per review:** `judge_latency_ms`, `judge_tokens`, `judge_cost_usd`, `judge_score` $=\bar s$, `judge_confidence` $=c$.

### 3.7 Backends
All backends implement `complete(msgs, tools, max_tokens, temperature) → response`, using the stdlib
HTTP client.

| `backend` | Use | Notes |
|---|---|---|
| `stub` | tests, judges' runs without a key | deterministic, see below |
| `ollama` | local model (brief §7) | `http://127.0.0.1:11434`, model from config |
| `anthropic` | paid API when `ANTHROPIC_API_KEY` is set | default `claude-haiku-4-5-20251001` for cost |

The **stub** computes its probabilities from the digest alone. It never calls tools, and all its samples
are identical, so $c=1$. It uses four features in $[0,1]$:

$$
\begin{aligned}
f_1&=\tfrac{\#\{\text{steps with tool}\notin\text{allowed\_tools}\}}{\#\text{steps}}, &
f_2&=[\exists\, s:\ \neg\text{in\_scope}_s],\\
f_3&=[\exists\, s<s':\ \text{result\_trust}_s=\text{untrusted}\ \wedge\ \text{side\_effect}_{s'}\neq\text{read}], &
f_4&=[\exists\, s:\ \text{reason\_family}_s\in\{\text{CONTENT},\text{EGRESS},\text{SUPPLY\_CHAIN}\}],
\end{aligned}
$$

$$
p^{\text{drift}}=1-(1-w_1f_1)(1-w_2f_2),\qquad
p^{\text{inj}}=1-(1-w_3f_3)(1-w_4f_4).
$$

The weights $w=(0.6,\,0.5,\,0.4,\,0.5)$ are set in config. The stub makes the plugin's control flow, budgets
and Layer 1 effects fully testable. It is **not** claimed to be a semantic detector.

### 3.8 Failure handling
Transport errors, timeouts (`timeout_s`, default 20), invalid JSON and too few valid samples all make the
review **unavailable**. The result is finding `judge.unavailable` (low, confidence 1), metric
`judge_errors`, and no evidence or trust change. The judge is asynchronous, so this is deliberately
fail-open: a provider outage must not halt every session, while the inline layer stays fail-closed.

### 3.9 Parameters (policy file, `intercept.goal_judge`)

| Key | Default | Meaning |
|---|---|---|
| `backend` / `model` | `stub` / – | §3.7 |
| `samples` $k$, `temperature` $\theta$ | 3, 0.7 | self-consistency |
| `max_turns` $T_{\max}$, `max_tokens` $M$ | 3, 400 | loop bounds |
| `every_n_tools` $k_{\text{every}}$ | 5 | periodic trigger |
| `allow_content` | true (strict: false) | enables `get_content_excerpt` |
| `session_budget_usd` $B_s$, `daily_budget_usd` $B_d$ | 0.05, 2.00 | judge spend limits |
| `max_llr_per_signal` $L^{\text{sem}}_{\max}$ | 1.39 (≈ ln 4) | per-signal cap |
| `stub_weights` $w$ | (0.6, 0.5, 0.4, 0.5) | stub only |
| thresholds | from `intercept.semantic_guard` | §3.5 |

---

## 4. Layer 1 session trust: fusing the evidence

### 4.1 State and update
For each session the gateway keeps the log-odds that the session is compromised:

$$
z_0=\operatorname{logit}(\pi_0),\qquad
\text{trust}=1-\sigma(z)=\Pr(\text{not compromised}\mid\text{evidence}).
$$

A `LOWER_TRUST` signal with evidence $e>0$ from plugin $p$ updates the state (Bayes' rule in log-odds
form, treating the pieces of evidence as conditionally independent):

$$
z\leftarrow z+\tilde e,\qquad
\tilde e=\begin{cases}
\min(e,\,L_{\max}) & p\text{ deterministic}\\
\min\bigl(e,\,L^{\text{sem}}_{\max},\,C_{\text{sem}}-E_{\text{sem}}\bigr)^{+},\quad E_{\text{sem}}\leftarrow E_{\text{sem}}+\tilde e & p\text{ semantic}
\end{cases}
$$

Here $E_{\text{sem}}$ is the semantic evidence accepted so far in the session. Signals with $e\le0$ are
rejected, so trust is **monotone non-increasing** within a session, consistent with the tighten-only
feedback lattice. Duplicate `signal_id`s are ignored.

### 4.2 Tiers
The policy gives thresholds $1>\theta_{\text{watch}}>\theta_{\text{restr}}>\theta_{\text{quar}}>\theta_{\text{halt}}>0$.
The tier is the most severe one whose threshold trust has fallen below. Entering a tier applies its effect
once, and effects only accumulate:

| Tier (trust <) | Standard | Effect on the session (`_Run` in `intercept/governed/gateway.py`) |
|---|---|---|
| watch | 0.80 | trace annotation and ALERT |
| restricted | 0.50 | write/irreversible tools → `approval_tools`; egress tools → `blocked_tools` |
| quarantine | 0.25 | `strict = True` (every non-read needs approval; LLM calls denied) |
| halt | 0.10 | `halted = True` |

Equivalently, trust $<\theta$ exactly when $z>\operatorname{logit}(1-\theta)$. With $\pi_0=0.02$
($z_0=-3.892$), the total evidence needed to reach each tier is:
- watch: $-1.386+3.892=2.51$;
- restricted: $0+3.892=3.89$;
- quarantine: $1.099+3.892=4.99$;
- halt: $2.197+3.892=6.09$.

### 4.3 Semantic cap
The rule "a semantic evaluator alone can quarantine but never halt" fixes the admissible range of the
semantic cap:

$$
\operatorname{logit}(1-\theta_{\text{quar}})-\operatorname{logit}(\pi_0)\ \le\ C_{\text{sem}}\ <\ \operatorname{logit}(1-\theta_{\text{halt}})-\operatorname{logit}(\pi_0).
$$

For the standard preset this gives $4.99\le C_{\text{sem}}<6.09$; we use $C_{\text{sem}}=5.5$. The
configuration loader rejects any preset that violates this inequality.

### 4.4 Worked example (standard preset)
ONB-17: an injected note makes the agent skip screening and attempt `create_client`.
1. `process-conformance` alarms at $S=6.25$ and sends $e=\min(6.25,3.0)=3.0$. Then $z=-0.892$ and trust $=0.71$: **watch**.
2. The judge, triggered by the write attempt, returns $\bar s=0.86$ with $c=0.93$. Since $\operatorname{logit}(0.86)+3.892=5.71$, the evidence is $e=0.93\cdot\min(5.71,1.39)=1.29$. Then $z=0.40$ and trust $=0.40$: **restricted**. Every later write needs approval, and egress is blocked.
3. Further evidence from either evaluator moves the session to quarantine. Only deterministic evidence can move it to halt.

### 4.5 Audit and telemetry
- **Audit:** every applied signal is persisted as a control event carrying `source_plugin`, the trigger event, $\tilde e$, trust before and after, and the tier.
- **Trace:** every gateway decision logs `trust=` and `tier=` in `action.decided`.
- **Dashboard:** a per-session trust timeline is built from the control events.

---

## 5. Test obligations
Each obligation has a positive (allowed) and a negative (flagged) side.

| Component | Positive (allowed / no signal) | Negative (flagged / tightened) |
|---|---|---|
| process-conformance | every clean scenario keeps $\max_t S_t<h-m$ | ONB-03 skip screening, ONB-12 loop, bait after `fetch_url`, hijack after a 20-step benign prefix all alarm; replaying an event yields an identical proposal |
| goal-judge (stub) | aligned session gives no finding; budget never exceeded | out-of-scope ids or untrusted→write gives band ≥ alert; malformed backend JSON gives `judge.unavailable` and no trust change; severity ≤ high; loop stops at $T_{\max}$ |
| trust (L1) | clean session: trust $=1-\pi_0$, all ALLOW | tier walk watch → restricted → quarantine → halt with the effects of §4.2; semantic-only evidence saturates at $C_{\text{sem}}$ and never halts; $e\le0$ rejected; duplicate `signal_id` ignored |
