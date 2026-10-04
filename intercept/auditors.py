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
    "aws_access_key": re.compile(r"\bAKIA[A-Z2-7]{16}\b"),
    "private_key": re.compile(r"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----", re.DOTALL),
    "api_key": re.compile(r"\bpgw_live_[A-Za-z0-9]+\b"),
}


def _contains_literal(value, pattern):
    return bool(re.search(re.escape(pattern), value, re.IGNORECASE))


def _redact_literal(value, pattern):
    new_value, count = re.subn(re.escape(pattern), "[REDACTED]", value, flags=re.IGNORECASE)
    return new_value, count > 0


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
            if set(c) != {"patterns", "action"} or c["action"] not in ("BLOCK", "REDACT", "ALERT") or not isinstance(c["patterns"], list):
                raise ValueError("invalid scanner")
            # Literal signatures, not untrusted regexes with catastrophic backtracking.
            if any(not isinstance(x, str) or not 1 <= len(x) <= 256 for x in c["patterns"]):
                raise ValueError("invalid literal signature")
        elif spec["type"] == "classified_scanner":
            if set(c) != {"classes", "action"} or c["action"] not in ("BLOCK", "REDACT", "ALERT"):
                raise ValueError("invalid classified scanner")
            if not isinstance(c["classes"], list) or not c["classes"] or any(x not in CLASSIFIED for x in c["classes"]):
                raise ValueError("unknown privacy or secret class")
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
    def __init__(self, specs):
        validate_specs(specs)
        self.specs = copy.deepcopy(specs)

    async def evaluate(self, action):
        checked = copy.deepcopy(action)
        evidence = []
        blocked = approval = False
        changed = False
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
