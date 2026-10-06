"""Config-selected auditors. Raw payloads are transient, never audit evidence."""
import asyncio
import copy
import json
import re
import time
import urllib.request
from urllib.parse import urlsplit

DECISIONS = {"ALLOW", "BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"}
CLASSIFIED = {
    # Operator-selected classes. The expressions live in code, not in the policy file.
    "pesel": re.compile(r"\b\d{11}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "aws_access_key": re.compile(r"\b(AKIA|ASIA)[A-Z2-7]{16}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
    "api_key": re.compile(r"\bpgw_live_[A-Za-z0-9]+\b"),
}
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
HOST_TOKEN = re.compile(r"(?i)(?<![a-z0-9-])(?:[a-z0-9-]{1,63}\.)+[a-z0-9-]{2,63}")


def _hosts(value):
    """Hostnames named in a string: URL hosts, email domains and bare domain tokens."""
    found = set()
    for token in re.split(r"[\s\"'<>()\[\],;]+", value):
        if "://" in token:
            try:
                token = urlsplit(token).hostname or ""
            except ValueError:
                continue
        token = token.rpartition("@")[2]
        try:  # internationalized names are compared in their punycode form
            token = token.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        found.update(match.lower().rstrip(".") for match in HOST_TOKEN.findall(token))
    return found


def _blocklisted(host, domains):
    labels = host.split(".")
    return any(".".join(labels[i:]) in domains for i in range(len(labels) - 1))


def _contains_literal(value, pattern):
    return pattern.casefold() in value.casefold()


def _find_casefold_spans(value: str, pattern: str) -> list[tuple[int, int]]:
    folded_pattern = pattern.casefold()
    if not folded_pattern:
        return []
    char_map: list[int] = []
    folded_chars: list[str] = []
    for orig_idx, char in enumerate(value):
        folded_char = char.casefold()
        for _ in folded_char:
            char_map.append(orig_idx)
        folded_chars.append(folded_char)
    folded_value = "".join(folded_chars)
    spans: list[tuple[int, int]] = []
    start = 0
    pattern_len = len(folded_pattern)
    while True:
        idx = folded_value.find(folded_pattern, start)
        if idx == -1:
            break
        orig_start = char_map[idx]
        orig_end = char_map[idx + pattern_len - 1] + 1
        if spans and orig_start < spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], orig_end))
        else:
            spans.append((orig_start, orig_end))
        start = idx + pattern_len
    return spans


def _redact_literal(value: str, pattern: str) -> tuple[str, bool]:
    spans = _find_casefold_spans(value, pattern)
    if not spans:
        return value, False
    parts = []
    last_end = 0
    for start, end in spans:
        parts.append(value[last_end:start])
        parts.append("[REDACTED]")
        last_end = end
    parts.append(value[last_end:])
    return "".join(parts), True



def validate_specs(specs):
    if not isinstance(specs, list) or len(specs) > 32:
        raise ValueError("auditors must be a bounded list")
    ids = set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) != {"id", "type", "config"}:
            raise ValueError("invalid auditor configuration")
        if not isinstance(spec["id"], str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", spec["id"]) or spec["id"] in ids:
            raise ValueError("invalid/duplicate auditor id")
        ids.add(spec["id"])
        c = spec["config"]
        if not isinstance(c, dict):
            raise ValueError("auditor config must be an object")
        if spec["type"] == "tool_allowlist":
            if set(c) != {"allowed_tools"} or not isinstance(c["allowed_tools"], list) or any(not isinstance(x, str) for x in c["allowed_tools"]):
                raise ValueError("invalid allowlist")
        elif spec["type"] == "pattern_scanner":
            if set(c) != {"patterns", "action"} or c["action"] not in ("BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT") or not isinstance(c["patterns"], list):
                raise ValueError("invalid scanner")
            # Literal signatures, not untrusted regexes with catastrophic backtracking.
            if any(not isinstance(x, str) or not 1 <= len(x) <= 256 for x in c["patterns"]):
                raise ValueError("invalid literal signature")
        elif spec["type"] == "classified_scanner":
            if set(c) != {"classes", "action"} or c["action"] not in ("BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"):
                raise ValueError("invalid classified scanner")
            if not isinstance(c["classes"], list) or not c["classes"] or any(x not in CLASSIFIED for x in c["classes"]):
                raise ValueError("unknown privacy or secret class")
        elif spec["type"] == "domain_blocklist":
            if set(c) != {"domains", "action"} or c["action"] not in ("BLOCK", "REQUIRE_APPROVAL", "ALERT"):
                raise ValueError("invalid domain blocklist")
            if (not isinstance(c["domains"], list) or len(c["domains"]) > 512
                    or any(not isinstance(d, str) or len(d) > 253 or not DOMAIN.fullmatch(d) for d in c["domains"])):
                raise ValueError("blocklist entries must be lowercase hostnames")
        elif spec["type"] == "webhook":
            if set(c) != {"endpoint", "timeout_ms", "on_failure"}:
                raise ValueError("invalid webhook")
            u = urlsplit(c["endpoint"])
            if u.scheme != "http" or u.hostname != "127.0.0.1" or u.username or u.password or u.fragment:
                raise ValueError("initial webhooks must be loopback HTTP endpoints")
            if type(c["timeout_ms"]) is not int or not 1 <= c["timeout_ms"] <= 1000 or c["on_failure"] != "BLOCK":
                raise ValueError("invalid webhook failure policy")
        else:
            raise ValueError("unknown auditor type")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def webhook(config, action):
    req = urllib.request.Request(config["endpoint"], json.dumps(action).encode(), {"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=config["timeout_ms"] / 1000) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError("auditor response too large")
    result = json.loads(data)
    if not isinstance(result, dict) or set(result) != {"decision"} or result["decision"] not in DECISIONS - {"REDACT"}:
        raise ValueError("invalid auditor response")
    return result["decision"]


class Pipeline:
    def __init__(self, specs, *, plugin_config=None, contract=None, policy_level="standard", plugins=None):
        validate_specs(specs)
        self.specs = copy.deepcopy(specs)
        from intercept.plugins import load_plugins
        self.plugin_config = copy.deepcopy(plugin_config or {})
        self.plugins = list(plugins) if plugins is not None else load_plugins(self.plugin_config)
        self.contract = contract
        self.policy_level = policy_level

    def for_prompts(self):
        return Pipeline([s for s in self.specs if s["type"] != "tool_allowlist"],
                        plugin_config=self.plugin_config, contract=self.contract,
                        policy_level=self.policy_level, plugins=self.plugins)

    def _budget_plugin(self):
        return next((item for item in self.plugins if item.name == "budget-guard"), None)

    def reserve_budget(self, action, *, action_type="tool_call", tokens=0, tool_calls=0, known_tokens=0):
        plugin = self._budget_plugin()
        if plugin is None:
            return None
        return self._budget_row(plugin, action, action_type, tokens=tokens,
                                tool_calls=tool_calls, known_tokens=known_tokens)

    def report_budget_limit(self, action, code):
        plugin = self._budget_plugin()
        if plugin is None:
            return None
        result = plugin.report_limit(action["session_id"], code)
        return self._budget_row_from_result(plugin, result)

    def _budget_row(self, plugin, action, action_type, **usage):
        contract = self.contract.to_dict() if self.contract is not None else {}
        from intercept.plugins import AuditContext
        ctx = AuditContext(
            trace_id=contract.get("run_id") or action["session_id"],
            session_id=action["session_id"], agent_id=contract.get("agent_id", "unbound_policy"),
            action_type=action_type, action_name=action["tool"], payload=copy.deepcopy(action["arguments"]),
            current_policy_level=self.policy_level, task_contract=copy.deepcopy(contract),
            policy_version=contract.get("policy_version", "unbound_policy"), action_id=action["call_id"],
        )
        started = time.monotonic()
        result = plugin.reserve(ctx, **usage)
        return self._budget_row_from_result(plugin, result, started)

    @staticmethod
    def _budget_row_from_result(plugin, result, started=None):
        return {"auditor": plugin.name, "decision": result.decision,
                "code": result.violation_code or "BUDGET_RESERVED",
                "latency_ms": round((time.monotonic() - started) * 1000, 3) if started else 0.0,
                "evidence": dict(result.evidence)}

    async def evaluate_output(self, action):
        return await self.evaluate(action, phase="output")

    async def evaluate_prompt(self, action, *, phase="input"):
        return await self.evaluate(action, phase=phase, action_type="llm_call")

    async def evaluate(self, action, *, phase="input", action_type="tool_call"):
        checked = copy.deepcopy(action)
        evidence = []
        blocked = approval = False
        changed = False
        from intercept.plugins import AuditContext, AuditDecision
        contract = self.contract.to_dict() if self.contract is not None else {}
        for plugin in self.plugins:
            start = time.monotonic()
            ctx = AuditContext(
                trace_id=contract.get("run_id") or checked["session_id"],
                session_id=checked["session_id"], agent_id=contract.get("agent_id", "unbound_policy"),
                action_type=action_type, action_name=checked["tool"], payload=copy.deepcopy(checked["arguments"]),
                current_policy_level=self.policy_level, task_contract=copy.deepcopy(contract),
                policy_version=contract.get("policy_version", "unbound_policy"),
                action_id=checked["call_id"], phase=phase,
            )
            try:
                result = plugin.evaluate(ctx)
                if (not isinstance(result, AuditDecision)
                        or result.decision not in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL", "ALERT"}
                        or result.modified_payload is not None
                        or (result.violation_code is not None and (
                            not isinstance(result.violation_code, str)
                            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", result.violation_code)))
                        or any(key not in {"pattern_index", "calls_in_window", "tokens_used", "tokens_limit",
                                           "tool_calls_used", "tool_calls_limit"} or type(value) is not int
                               or not 0 <= value <= (1_000_000 if key.startswith("tokens_") else 10_001)
                               for key, value in result.evidence.items())):
                    raise ValueError("invalid plugin decision")
            except Exception:
                result = AuditDecision("BLOCK", violation_code="INTERCEPT_PLUGIN_FAILED")
            row = {"auditor": plugin.name, "decision": result.decision,
                   "code": result.violation_code or "PASS",
                   "latency_ms": round((time.monotonic() - start) * 1000, 3)}
            if result.evidence:
                row["evidence"] = dict(result.evidence)
            if "pattern_index" in result.evidence:
                row["rule_id"] = f"regex.pattern.{result.evidence['pattern_index']}"
            if result.decision != "ALLOW" and plugin.name == "budget-guard":
                row["rule_id"] = f"budget.{result.violation_code.lower()}"
            evidence.append(row)
            blocked |= result.decision == "BLOCK"
            approval |= result.decision == "REQUIRE_APPROVAL"
        for spec in self.specs:
            start = time.monotonic()
            config = spec["config"]
            decision = "ALLOW"
            code = "PASS"
            if spec["type"] == "tool_allowlist":
                if checked["tool"] not in config["allowed_tools"]:
                    decision, code = "BLOCK", "TOOL_DENIED"
            elif spec["type"] in ("pattern_scanner", "classified_scanner"):
                matched = False

                def scan(value):
                    nonlocal matched
                    if isinstance(value, str):
                        if spec["type"] == "pattern_scanner":
                            for pattern in config["patterns"]:
                                if _contains_literal(value, pattern):
                                    matched = True
                                    if config["action"] == "REDACT":
                                        value, _ = _redact_literal(value, pattern)
                        else:
                            for name in config["classes"]:
                                expression = CLASSIFIED[name]
                                if expression.search(value):
                                    matched = True
                                    if config["action"] == "REDACT":
                                        value = expression.sub("[REDACTED]", value)
                        return value
                    if isinstance(value, list):
                        return [scan(v) for v in value]
                    if isinstance(value, dict):
                        # Keys cannot safely be rewritten; a matching key denies instead.
                        if spec["type"] == "pattern_scanner" and any(
                                any(_contains_literal(k, p) for p in config["patterns"]) for k in value):
                            raise ValueError("signature in argument key")
                        if spec["type"] == "classified_scanner" and any(
                                any(CLASSIFIED[name].search(k) for name in config["classes"]) for k in value):
                            raise ValueError("signature in argument key")
                        # The account IBAN is issued by the gateway. Free-text IBAN values are still redacted.
                        return {k: v if k == "iban" and spec["type"] == "classified_scanner" else scan(v)
                                for k, v in value.items()}
                    return value

                try:
                    checked["arguments"] = scan(checked["arguments"])
                    if matched:
                        decision, code = config["action"], "SIGNATURE_MATCH" if spec["type"] == "pattern_scanner" else "PRIVACY_MATCH"
                        changed |= decision == "REDACT"
                except ValueError:
                    decision, code = "BLOCK", "SIGNATURE_IN_KEY"
            elif spec["type"] == "domain_blocklist":
                domains = frozenset(config["domains"])

                def strings(value):
                    if isinstance(value, str):
                        yield value
                    elif isinstance(value, list):
                        for v in value:
                            yield from strings(v)
                    elif isinstance(value, dict):
                        for k, v in value.items():
                            yield from strings(k)
                            yield from strings(v)

                if any(_blocklisted(host, domains) for text in strings(checked["arguments"]) for host in _hosts(text)):
                    decision, code = config["action"], "DOMAIN_BLOCKLISTED"
            elif blocked:
                # Semantic/external work never needs to run after a hard deny.
                code = "SKIPPED_HARD_DENY"
            else:
                try:
                    decision = await asyncio.wait_for(asyncio.to_thread(webhook, config, checked), config["timeout_ms"] / 1000)
                except Exception:
                    decision, code = "BLOCK", "AUDITOR_UNAVAILABLE"
            blocked |= decision == "BLOCK"
            approval |= decision == "REQUIRE_APPROVAL"
            evidence.append({"auditor": spec["id"], "decision": decision, "code": code,
                             "latency_ms": round((time.monotonic() - start) * 1000, 3)})
        return checked, evidence, "BLOCK" if blocked else "REQUIRE_APPROVAL" if approval else "ALLOW", changed
