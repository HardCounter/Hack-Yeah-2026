"""Pre-dispatch regex blacklist. RE2 avoids backtracking-based resource exhaustion."""
from collections.abc import Mapping

import re2

from intercept.plugins import AuditDecision

MAX_PATTERNS = 64
MAX_PATTERN_LENGTH = 256
MAX_INPUT_BYTES = 65536
MAX_NODES = 4096
MAX_DEPTH = 32


def compile_patterns(patterns):
    if not isinstance(patterns, list) or len(patterns) > MAX_PATTERNS:
        raise ValueError("patterns must be a bounded list")
    options = re2.Options()
    options.log_errors = False  # engine diagnostics can echo raw regex configuration
    options.max_mem = 1 << 20
    compiled = []
    for pattern in patterns:
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_PATTERN_LENGTH:
            raise ValueError("invalid pattern length")
        try:
            compiled.append(re2.compile(pattern, options=options))
        except (re2.error, UnicodeError):
            raise ValueError("invalid or unsupported RE2 pattern") from None
    return tuple(compiled)


class PatternMatch:
    name = "pattern-match"
    version = "1.0.0"
    method = "deterministic"

    def setup(self, config):
        if not isinstance(config, Mapping) or set(config) - {"enabled", "patterns", "fields", "action"}:
            raise ValueError("invalid pattern-match configuration")
        if type(config.get("enabled", True)) is not bool or config.get("action", "BLOCK") != "BLOCK":
            raise ValueError("pattern-match must block")
        fields = config.get("fields", ["tool", "arguments"])
        if (not isinstance(fields, list) or not fields
                or any(not isinstance(field, str) or field not in {"tool", "arguments"} for field in fields)
                or len(fields) != len(set(fields))):
            raise ValueError("invalid matching fields")
        self.fields = tuple(fields)
        self.patterns = compile_patterns(config.get("patterns", []))

    def evaluate(self, ctx):
        # Output controls remain separate: a post-dispatch block cannot prevent that action.
        if ctx.phase != "input" or not self.patterns:
            return AuditDecision("ALLOW")
        roots = []
        if "tool" in self.fields:
            roots.append(ctx.action_name)
        if "arguments" in self.fields:
            roots.append(ctx.payload)
        stack = [(value, 0) for value in roots]
        nodes = size = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if nodes > MAX_NODES or depth > MAX_DEPTH:
                return AuditDecision("BLOCK", violation_code="REGEX_INPUT_LIMIT")
            if isinstance(value, str):
                try:
                    size += len(value.encode("utf-8"))
                except UnicodeError:
                    return AuditDecision("BLOCK", violation_code="REGEX_INPUT_INVALID")
                if size > MAX_INPUT_BYTES:
                    return AuditDecision("BLOCK", violation_code="REGEX_INPUT_LIMIT")
                for index, pattern in enumerate(self.patterns):
                    if pattern.search(value):
                        return AuditDecision("BLOCK", violation_code="REGEX_PATTERN_MATCH",
                                             evidence={"pattern_index": index})
            elif isinstance(value, dict):
                if len(value) * 2 + nodes + len(stack) > MAX_NODES:
                    return AuditDecision("BLOCK", violation_code="REGEX_INPUT_LIMIT")
                for key, child in value.items():
                    stack.append((key, depth + 1))
                    stack.append((child, depth + 1))
            elif isinstance(value, list):
                if len(value) + nodes + len(stack) > MAX_NODES:
                    return AuditDecision("BLOCK", violation_code="REGEX_INPUT_LIMIT")
                stack.extend((child, depth + 1) for child in value)
        return AuditDecision("ALLOW")


PLUGINS = [PatternMatch]
