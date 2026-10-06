"""Strict management API models. No executable plugin paths or credential fields."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, AfterValidator, model_validator

from intercept.policy.auditors import validate_specs

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")]
Revision = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Token = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:/-]+$")]
Action = Literal["BLOCK", "REDACT", "REQUIRE_APPROVAL", "ALERT"]
ModelIds = Annotated[list[Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]],
                     Field(min_length=1, max_length=100)]


def utc_timestamp(value):
    from persistence.models import parse_utc_iso_timestamp
    parse_utc_iso_timestamp(value)
    return value


Timestamp = Annotated[str, AfterValidator(utc_timestamp)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class BudgetConfig(Model):
    tokens: Annotated[int, Field(ge=1, le=1_000_000)]
    tool_calls: Annotated[int, Field(ge=1, le=1000)]
    cost_usd: Annotated[float, Field(ge=0, le=1000)] | None


class Controls(Model):
    email_recipients: Annotated[list[Annotated[str, Field(max_length=254, pattern=r"^[^\s@]+@[^\s@]+$")]], Field(max_length=100)]
    egress_hosts: Annotated[list[Annotated[str, Field(max_length=253, pattern=r"^[A-Za-z0-9.-]+$")]], Field(max_length=100)]
    model_source_hosts: Annotated[list[Annotated[str, Field(max_length=253, pattern=r"^[A-Za-z0-9.-]+$")]], Field(max_length=100)]
    allowed_model_suffixes: Annotated[list[Annotated[str, Field(max_length=40, pattern=r"^\.[A-Za-z0-9]+$")]], Field(max_length=20)]


class PatternConfig(Model):
    patterns: Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(max_length=256)]
    action: Action


class ClassifiedConfig(Model):
    classes: Annotated[list[Literal["pesel", "iban", "aws_access_key", "private_key", "api_key"]], Field(min_length=1, max_length=5)]
    action: Action


class AllowlistConfig(Model):
    allowed_tools: Annotated[list[Token], Field(max_length=100)]


class PatternAuditor(Model):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
    type: Literal["pattern_scanner"]
    config: PatternConfig


class ClassifiedAuditor(Model):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
    type: Literal["classified_scanner"]
    config: ClassifiedConfig


class AllowlistAuditor(Model):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
    type: Literal["tool_allowlist"]
    config: AllowlistConfig


class DomainBlocklistConfig(Model):
    # Hostname syntax is checked by intercept.policy.auditors.validate_specs.
    domains: Annotated[list[Annotated[str, Field(min_length=1, max_length=253)]], Field(max_length=512)]
    action: Literal["BLOCK", "REQUIRE_APPROVAL", "ALERT"]


class DomainBlocklistAuditor(Model):
    id: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,64}$")]
    type: Literal["domain_blocklist"]
    config: DomainBlocklistConfig


class VelocityGuardConfig(Model):
    enabled: bool = True
    window_s: Annotated[float, Field(gt=0, le=3600)]
    max_calls: Annotated[int, Field(ge=1, le=10_000)]


class PatternMatchConfig(Model):
    enabled: bool = True
    patterns: Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(max_length=64)]
    fields: Annotated[list[Literal["tool", "arguments"]], Field(min_length=1, max_length=2)] = ["tool", "arguments"]
    action: Literal["BLOCK"] = "BLOCK"

    @model_validator(mode="after")
    def valid_regexes(self):
        from plugins.pattern_match import PatternMatch
        PatternMatch().setup(self.model_dump())
        return self


class SemanticGuardConfig(Model):
    block_threshold: Annotated[float, Field(ge=0, le=1)]
    approve_threshold: Annotated[float, Field(ge=0, le=1)]
    alert_threshold: Annotated[float, Field(ge=0, le=1)]
    on_error: Literal["BLOCK"]
    allowed_models: ModelIds  # judge models the semantic guard may use

    @model_validator(mode="after")
    def ordered(self):
        if not self.alert_threshold <= self.approve_threshold <= self.block_threshold:
            raise ValueError("unordered thresholds")
        return self


Adjustment = Literal["ALERT", "REQUIRE_APPROVAL_FOR", "BLOCK_TOOLS", "STRICT_MODE", "HALT_SESSION"]


class FeedbackConfig(Model):
    enabled: bool
    allow_agent_scope: Literal[False]
    max_ttl_s: Annotated[int, Field(ge=1, le=86400)]
    max_signals_per_session_per_minute: Annotated[int, Field(ge=1, le=1000)]
    allowed_actions: dict[Literal["trajectory-risk", "velocity-guard"], Annotated[list[Adjustment], Field(max_length=5)]]


class InterceptConfig(Model):
    # This iteration reserves trajectory settings but does not configure the consume plane.
    trajectory_risk: Model
    velocity_guard: VelocityGuardConfig
    pattern_match: PatternMatchConfig | None = None
    feedback: FeedbackConfig
    semantic_guard: SemanticGuardConfig


class PolicyConfig(Model):
    name: Name
    description: Annotated[str, Field(max_length=512)]
    allowed_tools: Annotated[list[Token], Field(min_length=1, max_length=100)]
    admin_tools: Annotated[list[Token], Field(max_length=100)]
    budget: BudgetConfig
    allowed_models: ModelIds  # models agents may call
    max_output_tokens: Annotated[int, Field(ge=1, le=8192)]
    require_approval: Annotated[list[Token], Field(max_length=100)]
    feed_version: Token
    controls: Controls
    auditors: Annotated[list[Annotated[PatternAuditor | ClassifiedAuditor | DomainBlocklistAuditor | AllowlistAuditor,
                                     Field(discriminator="type")]], Field(max_length=32)]
    intercept: InterceptConfig

    @model_validator(mode="after")
    def policy_constraints(self):
        from simulation import agent
        from persistence.privacy import token
        known = set(agent.registry.REGISTRY)
        allowed = set(self.allowed_tools) | set(self.admin_tools)
        if not allowed <= known or not set(self.require_approval) <= allowed:
            raise ValueError("unknown or out-of-scope tools")
        judge_models = self.intercept.semantic_guard.allowed_models
        for values in (self.allowed_tools, self.admin_tools, self.require_approval, self.allowed_models, judge_models):
            if len(values) != len(set(values)):
                raise ValueError("duplicate entries")
        specs = [auditor.model_dump() for auditor in self.auditors]
        validate_specs(specs)
        token(self.feed_version, required=True)
        for model in (*self.allowed_models, *judge_models):
            token(model, required=True)
        for spec in specs:
            token(spec["id"], required=True)
            if spec["type"] == "tool_allowlist" and not set(spec["config"]["allowed_tools"]) <= known:
                raise ValueError("unknown auditor tool")
        return self


class ConfigUpdateResult(Model):
    name: Name
    revision: Revision
    updated_at: Timestamp
    selected: bool
    active_revision: Revision
    requires_selection: bool


class ConfigSelectionRequest(Model):
    name: Name
    revision: Revision


class ConfigSelectionResult(Model):
    name: Name
    revision: Revision
    selected_at: Timestamp
    effective_for: Literal["new_sessions"] = "new_sessions"


class ConfigSummary(Model):
    name: Name
    preset: bool
    description: str
    revision: Revision
    active_revision: Revision | None = None
    selected: bool
    requires_selection: bool


class ErrorDetail(Model):
    code: str
    message: str
    details: dict[str, list[str]]


class ErrorResponse(Model):
    error: ErrorDetail


class StoredConfig(Model):
    config: PolicyConfig
    revision: Revision
    updated_at: Timestamp


class SelectedSnapshot(Model):
    name: Name
    revision: Revision
    selected_at: Timestamp
    config: PolicyConfig


class ConfigChange(Model):
    operation: Literal["update", "select"]
    name: Name
    revision: Revision
    ts: Timestamp


class ConfigState(Model):
    schema_version: Literal[1]
    configs: dict[Literal["lenient", "standard", "strict"], StoredConfig]
    selection: SelectedSnapshot
    changes: Annotated[list[ConfigChange], Field(max_length=1000)]
