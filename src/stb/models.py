from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stb import API_VERSION


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Status(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class TimeUnit(str, Enum):
    S = "s"
    MS = "ms"
    US = "us"
    NS = "ns"
    PS = "ps"
    FS = "fs"


class TimeSpec(StrictModel):
    value: str
    unit: TimeUnit


class TimePoint(StrictModel):
    ticks: str
    unit: TimeUnit
    raw_ticks: str | None = None
    raw_scale_unit: str | None = None


class ObjectRef(StrictModel):
    model: Literal["netlist", "language", "waveform"]
    context_id: str
    worker_generation: int = Field(ge=1)
    npi_type: str
    full_name: str | None = None
    object_id: str | None = None

    @model_validator(mode="after")
    def require_identity(self) -> "ObjectRef":
        if not self.full_name and not self.object_id:
            raise ValueError("full_name or object_id is required")
        return self


class SourceLocation(StrictModel):
    file: str
    begin_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    include_chain: list[str] = Field(default_factory=list)


class ObjectSummary(StrictModel):
    ref: ObjectRef
    name: str | None = None
    semantic_class: str | None = None
    classification_rule: str | None = None
    source: SourceLocation | None = None
    description: str | None = None


class LogicValue(StrictModel):
    kind: Literal["logic"] = "logic"
    width: int = Field(ge=1)
    encoding: Literal["bin"] = "bin"
    value: str
    signed: bool | None = None


class RealValue(StrictModel):
    kind: Literal["real"] = "real"
    value: str


class StringValue(StrictModel):
    kind: Literal["string"] = "string"
    value: str


class EnumValue(StrictModel):
    kind: Literal["enum"] = "enum"
    literal: str
    symbol: str | None = None


TypedValue = LogicValue | RealValue | StringValue | EnumValue


class LimitsReceipt(StrictModel):
    truncated: bool = False
    termination_reason: str | None = None
    scanned: int | None = None
    returned: int | None = None
    next_cursor: str | None = None


class MetricsReceipt(StrictModel):
    duration_ms: float = Field(ge=0)
    total_ms: float = Field(default=0, ge=0)
    queue_ms: float = Field(default=0, ge=0)
    npi_ms: float = Field(default=0, ge=0)
    python_ms: float = Field(default=0, ge=0)
    serialization_ms: float = Field(default=0, ge=0)
    transport_ms: float = Field(default=0, ge=0)
    input_bytes: int = Field(default=0, ge=0)
    response_bytes: int = Field(default=0, ge=0)
    npi_calls: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)


class Receipt(StrictModel):
    api_version: Literal["stb.v1"] = API_VERSION
    request_id: str
    context_id: str | None = None
    worker_generation: int | None = None
    design_fingerprint: str | None = None
    wave_id: str | None = None
    wave_generation: int | None = None
    verdi_version: str | None = None
    verdi_compatibility: Literal["verified", "unverified"] | None = None
    backend: str
    limits: LimitsReceipt = Field(default_factory=LimitsReceipt)
    metrics: MetricsReceipt


class ItemResult(StrictModel):
    input_index: int = Field(ge=0)
    ok: bool
    data: Any = None
    error_code: str | None = None
    error: dict[str, Any] | None = None


class Response(StrictModel):
    status: Status
    data: Any = None
    items: list[ItemResult] | None = None
    summary: dict[str, int] | None = None
    error: dict[str, Any] | None = None
    receipt: Receipt
