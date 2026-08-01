from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from stb.errors import StbError
from stb.models import ObjectRef, StrictModel


class DesignSpec(StrictModel):
    argv: list[str]
    cwd: str | None = None
    top: str | None = None
    env_refs: list[str] = Field(default_factory=list)
    launcher: Literal["local"] = "local"


class WaveSpec(StrictModel):
    wave_id: str
    path: str


JsonScalar: TypeAlias = str | int | float | bool | None


class CatalogFilters(StrictModel):
    status: Literal["supported", "deferred", "excluded"] | None = None
    model: str | None = None
    class_name: str | None = Field(default=None, alias="class")
    contains: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: str | None = None
    include_all: bool = False


class WhereCompare(StrictModel):
    op: Literal["eq", "ne", "lt", "le", "gt", "ge", "glob", "regex"]
    property: str = Field(min_length=1)
    value: JsonScalar


class WhereMembership(StrictModel):
    op: Literal["in", "not_in"]
    property: str = Field(min_length=1)
    value: list[JsonScalar] = Field(min_length=1)


class WhereExists(StrictModel):
    op: Literal["exists"]
    property: str = Field(min_length=1)
    value: bool = True


class WhereGroup(StrictModel):
    op: Literal["all", "any"]
    args: list["WhereNode"] = Field(min_length=1)


class WhereNot(StrictModel):
    op: Literal["not"]
    arg: "WhereNode"


WhereNode: TypeAlias = Annotated[
    WhereCompare | WhereMembership | WhereExists | WhereGroup | WhereNot,
    Field(discriminator="op"),
]
WhereAdapter = TypeAdapter(WhereNode)


class TraverseFilters(StrictModel):
    where: WhereNode


class ExprSignal(StrictModel):
    signal: str = Field(min_length=1)


class ExprLiteral(StrictModel):
    literal: str | int | bool


class ExprOp(StrictModel):
    op: Literal[
        "logic.eq",
        "logic.ne",
        "logic.and",
        "logic.or",
        "logic.not",
        "logic.is_known",
        "logic.is_x",
        "logic.is_z",
        "bit.and",
        "bit.or",
        "bit.xor",
        "bit.not",
    ]
    args: list["ExprNode"] = Field(default_factory=list)


ExprNode: TypeAlias = ExprSignal | ExprLiteral | ExprOp
ExprAdapter = TypeAdapter(ExprNode)


class ExprEnvelope(StrictModel):
    expr_version: Literal["stb.expr.v1"]
    root: ExprNode | None = None
    start: ExprNode | None = None
    end: ExprNode | None = None


class WaveSide(StrictModel):
    wave_id: str
    signal: str
    context_id: str | None = None


class ExactMappingRule(StrictModel):
    kind: Literal["exact"]
    pairs: dict[str, str] = Field(default_factory=dict)


class PrefixReplaceRule(StrictModel):
    kind: Literal["prefix_replace"]
    source_prefix: str
    target_prefix: str


class SeparatorNormalizeRule(StrictModel):
    kind: Literal["separator_normalize"]
    source: str = "/"
    target: str = "."


class RegexReplaceRule(StrictModel):
    kind: Literal["regex_replace"]
    pattern: str
    replacement: str = ""


class BitMappingPair(StrictModel):
    design: str
    waveform: str


class BitMappingRule(StrictModel):
    kind: Literal["bit_mapping"]
    pairs: list[BitMappingPair] = Field(default_factory=list)


MappingRule: TypeAlias = Annotated[
    ExactMappingRule
    | PrefixReplaceRule
    | SeparatorNormalizeRule
    | RegexReplaceRule
    | BitMappingRule,
    Field(discriminator="kind"),
]


class MappingProfile(StrictModel):
    rules: list[MappingRule] = Field(default_factory=list)


class MappingPair(StrictModel):
    design_full_name: str
    waveform_full_name: str


class ContextRequest(StrictModel):
    action: Literal["open", "reload", "close", "list", "status", "release_objects"]
    context_id: str | None = None
    backend: Literal["fake", "verdi"] | None = None
    design_spec: DesignSpec | None = None
    wave_specs: list[WaveSpec] | None = None
    object_ids: list[str] | None = None


class WaveManageRequest(StrictModel):
    action: Literal["attach", "reload", "detach", "list", "status"]
    context_id: str
    wave_id: str | None = None
    path: str | None = None


class CatalogRequest(StrictModel):
    context_id: str
    kind: str
    filters: CatalogFilters = Field(default_factory=CatalogFilters)


class ResolveRequest(StrictModel):
    model: Literal["netlist", "language", "waveform"] = "netlist"
    wave_id: str | None = None
    name: str
    npi_type: str | None = None
    object_id: str | None = None


class ObjectGetRequest(StrictModel):
    references: list[ObjectRef]
    properties: list[str] = Field(default_factory=list)
    wave_id: str | None = None
    include_available_relations: bool = False
    max_chars: int = Field(default=2000, ge=1)


class ObjectQueryRequest(StrictModel):
    model: Literal["netlist", "language", "waveform"] = "netlist"
    wave_id: str | None = None
    scope: str | None = None
    npi_types: list[str] = Field(default_factory=list)
    semantic_classes: list[str] = Field(default_factory=list)
    where: WhereNode | None = None
    limit: int = Field(default=100, ge=1, le=100_000)
    cursor: str | None = None
    allow_global: bool = False
    max_scan: int = Field(default=100_000, ge=1, le=1_000_000)


class TraverseRequest(StrictModel):
    roots: list[ObjectRef]
    relation: str
    depth: int = Field(default=1, ge=1, le=100)
    filters: TraverseFilters | WhereNode | None = None
    max_nodes: int = Field(default=1000, ge=1, le=100_000)
    cursor: str | None = None
    wave_id: str | None = None


class ConnectivityRequest(StrictModel):
    kind: Literal["driver", "load"]
    signals: list[str]
    bit_mode: Literal["aggregate", "expand"] = "aggregate"
    npi_type: str = "DECL_NET"


class TraceRequest(StrictModel):
    kind: Literal["driver", "load", "path", "fanin", "fanout"]
    roots: list[str | ObjectRef]
    targets: list[str | ObjectRef] = Field(default_factory=list)
    stop_at: list[str | ObjectRef] = Field(default_factory=list)
    max_depth: int = Field(default=20, ge=1)
    max_nodes: int = Field(default=1000, ge=1, le=100_000)


class TimedTraceRequest(StrictModel):
    signals: list[str]
    wave_id: str | None = None
    time: str
    max_nodes: int = Field(default=1000, ge=1, le=100_000)
    max_depth: int = Field(default=20, ge=1)


class WaveValueRequest(StrictModel):
    wave_id: str | None = None
    signals: list[str]
    times: list[str]


class WaveChangesRequest(StrictModel):
    wave_id: str | None = None
    signals: list[str]
    start: str
    end: str
    direction: Literal["forward", "backward"] = "forward"
    max_changes: int = Field(default=1000, ge=1, le=10_000_000)
    cursor: str | None = None


class WaveComputeRequest(StrictModel):
    operation: Literal[
        "sample",
        "find",
        "statistics",
        "compare",
        "first_divergence",
        "period",
        "pulse",
        "xz",
        "evaluate_window",
        "extract_events",
        "match_transactions",
    ]
    wave_id: str | None = None
    signals: list[str] = Field(default_factory=list)
    times: list[str] = Field(default_factory=list)
    start: str | None = None
    end: str | None = None
    value: str | None = None
    edge: str | None = None
    max_matches: int = Field(default=1000, ge=1)
    max_points: int = Field(default=10_000, ge=1, le=100_000)
    max_events: int = Field(default=1000, ge=1)
    left: WaveSide | None = None
    right: WaveSide | None = None
    context_mode: Literal["same", "cross"] = "same"
    cursor: str | None = None
    max_transitions: int = Field(default=1_000_000, ge=1, le=10_000_000)
    expression: ExprEnvelope | None = None

    @model_validator(mode="after")
    def require_operation_inputs(self) -> "WaveComputeRequest":
        def require_scalar(*names: str) -> None:
            missing = [name for name in names if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    f"{self.operation} requires {', '.join(sorted(missing))}"
                )

        def require_non_empty(*names: str) -> None:
            missing = [name for name in names if not getattr(self, name)]
            if missing:
                raise ValueError(
                    f"{self.operation} requires non-empty {', '.join(sorted(missing))}"
                )

        if self.operation == "sample":
            require_non_empty("signals", "times")
        elif self.operation in {"statistics", "xz", "period", "pulse"}:
            require_non_empty("signals")
            require_scalar("start", "end")
        elif self.operation == "find":
            require_non_empty("signals")
            require_scalar("start", "end", "value")
        elif self.operation == "compare":
            require_scalar("left", "right")
            require_non_empty("times")
        elif self.operation == "first_divergence":
            require_scalar("left", "right", "start", "end")
        elif self.operation in {"evaluate_window", "extract_events"}:
            require_scalar("start", "end", "expression")
            if self.expression is not None and self.expression.root is None:
                raise ValueError(f"{self.operation} requires expression.root")
        elif self.operation == "match_transactions":
            require_scalar("start", "end", "expression")
            if self.expression is not None and (
                self.expression.start is None or self.expression.end is None
            ):
                raise ValueError("match_transactions requires expression.start and expression.end")
        return self


class SourceContextRequest(StrictModel):
    reference: ObjectRef
    before_lines: int = Field(default=5, ge=0)
    after_lines: int = Field(default=5, ge=0)
    max_lines: int = Field(default=200, ge=1)
    max_chars: int = Field(default=16_384, ge=1)
    include_preprocessor: bool = False
    expansion_context_id: str | None = None
    allow_current_changed_source: bool = False


class AssertionStructureRequest(StrictModel):
    reference: ObjectRef
    max_source_lines: int = Field(default=200, ge=1, le=2000)
    max_chars: int = Field(default=65_536, ge=1, le=1_000_000)
    include_preprocessor: bool = True
    allow_current_changed_source: bool = False


class MappingRequest(StrictModel):
    action: Literal["resolve", "validate", "explain"]
    context_mode: Literal["same", "cross"] = "same"
    wave_id: str | None = None
    design_full_name: str | None = None
    waveform_full_name: str | None = None
    profile: MappingProfile | None = None
    pairs: list[MappingPair] | None = None

    @model_validator(mode="after")
    def require_action_inputs(self) -> "MappingRequest":
        if self.action in {"resolve", "explain"} and not self.design_full_name:
            raise ValueError(f"{self.action} requires design_full_name")
        return self


class ArtifactRequest(StrictModel):
    action: Literal["export", "status", "list", "cancel", "delete"]
    request: dict[str, Any] = Field(default_factory=dict)


CORE_PAYLOAD_MODELS = {
    "object_resolve": ResolveRequest,
    "object_get": ObjectGetRequest,
    "object_query": ObjectQueryRequest,
    "object_traverse": TraverseRequest,
    "connectivity_direct": ConnectivityRequest,
    "trace": TraceRequest,
    "trace_active_driver": TimedTraceRequest,
    "trace_value_origin": TimedTraceRequest,
    "wave_value": WaveValueRequest,
    "wave_changes": WaveChangesRequest,
    "wave_compute": WaveComputeRequest,
    "source_context": SourceContextRequest,
    "assertion_structure": AssertionStructureRequest,
    "mapping": MappingRequest,
}


def tool_json_schema(tool: str) -> dict[str, Any]:
    direct = {
        "context_manage": ContextRequest,
        "wave_manage": WaveManageRequest,
        "catalog": CatalogRequest,
        "artifact": ArtifactRequest,
    }
    if tool in direct:
        return direct[tool].model_json_schema()
    payload = CORE_PAYLOAD_MODELS.get(tool)
    if payload is None:
        return {
            "type": "object",
            "additionalProperties": True,
            "description": "Development tool schema is action-specific.",
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["context_id", "request"],
        "properties": {
            "context_id": {"type": "string", "minLength": 1},
            "request": payload.model_json_schema(),
        },
    }


def _validate(model: type[StrictModel], value: dict[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(value).model_dump(
            mode="python", exclude_none=True, by_alias=True
        )
    except ValidationError as exc:
        raise StbError(
            "invalid_request",
            "request schema validation failed",
            {"errors": exc.errors(include_url=False)},
        ) from exc


def validate_public_request(tool: str, request: dict[str, Any]) -> dict[str, Any]:
    direct = {
        "context_manage": ContextRequest,
        "wave_manage": WaveManageRequest,
        "catalog": CatalogRequest,
        "artifact": ArtifactRequest,
    }
    if tool in direct:
        return _validate(direct[tool], request)
    payload_model = CORE_PAYLOAD_MODELS.get(tool)
    if payload_model is None:
        return request
    context_id = request.get("context_id")
    if not isinstance(context_id, str) or not context_id:
        raise StbError("invalid_request", "context_id is required")
    if "request" in request:
        if set(request) != {"context_id", "request"}:
            raise StbError(
                "invalid_request",
                "wrapped worker request only accepts context_id and request",
            )
        payload = request["request"]
        if not isinstance(payload, dict):
            raise StbError("invalid_request", "request must be an object")
    else:
        payload = {key: value for key, value in request.items() if key != "context_id"}
    return {"context_id": context_id, "request": _validate(payload_model, payload)}
