# STB v1 API Contract

## 1. Common Request

```json
{
  "api_version": "stb.v1",
  "context_id": "rtl",
  "limits": {},
  "diagnostics": "none|summary|detailed"
}
```

Each MCP tool has a typed schema in addition to these common fields.

## 2. Common Response

```json
{
  "status": "complete|partial|failed",
  "data": {},
  "error": null,
  "receipt": {}
}
```

For batch tools:

```json
{
  "status": "partial",
  "items": [
    {"input_index": 0, "ok": true, "data": {}},
    {"input_index": 1, "ok": false, "error_code": "object_not_found"}
  ],
  "summary": {"requested": 2, "succeeded": 1, "failed": 1},
  "receipt": {}
}
```

## 3. Core Tool Inventory

### 3.1 `context_manage`

Actions:

- `open`
- `reload`
- `close`
- `status`
- `list`
- `release_objects`

Key inputs:

```text
action
context_id
design_spec?
wave_specs?
object_ids?
```

`design_spec` contains structured argv, cwd, top, launcher, and permitted
environment references. It is immutable within one worker generation.

### 3.2 `wave_manage`

Actions:

- `attach`
- `reload`
- `detach`
- `status`
- `list`

Key inputs:

```text
action
context_id
wave_id
path?
```

Multiple attached waves require explicit `wave_id` on waveform operations.

### 3.3 `catalog`

Kinds:

- models
- object_types
- semantic_classes
- properties
- relations
- operators
- wave_operations
- limits
- backend_capabilities

Catalog queries MUST support filters and pagination.

### 3.4 `object_resolve`

Inputs:

```text
model
name or reference
npi_type?
scope?
```

Returns exact match, candidate ambiguity, or stable not-found error. Fuzzy
selection is forbidden.

### 3.5 `object_get`

Inputs:

```text
references[]
properties[]
include_available_relations?
```

This is a batch tool. Unsupported properties are item-level errors unless the
property request itself is invalid.

### 3.6 `object_query`

Inputs:

```text
model
scope
npi_types[]
semantic_classes[]
where
limit
cursor?
allow_global?
```

`where` is a constrained declarative filter. Global search requires explicit
opt-in.

The canonical AST forms are:

```json
{"op":"eq","property":"range_size","value":32}
{"op":"all","args":[{"op":"glob","property":"name","value":"*_state"},{"op":"ne","property":"range_size","value":1}]}
{"op":"not","arg":{"op":"regex","property":"full_name","value":"\\.debug(_|$)"}}
```

### 3.7 `object_traverse`

Inputs:

```text
roots[]
relation
depth
filters?
max_nodes
cursor?
```

Depth `1` replaces a separate generic children tool. Supported relations are
discovered through `catalog`.

### 3.8 `connectivity_direct`

Inputs:

```text
kind: driver|load
signals[]
bit_mode: aggregate|expand
```

Returns one-hop connectivity only.

### 3.9 `trace`

Inputs:

```text
kind: driver|load|path|fanin|fanout
roots[]
targets[]?
stop_at[]
max_depth
max_nodes
summary?
```

Returns an evidence graph and explicit terminal reasons.

### 3.10 `trace_active_driver`

Inputs:

```text
signals[]
wave_id
time
time_semantics?
max_depth
max_nodes
```

Returns structural graph, active/feasible branch annotations, branch-layer
evidence, temporal resolution, and simulation override state.

### 3.11 `trace_value_origin`

Inputs:

```text
signals[]
wave_id
time
max_cycles?
max_time?
max_depth
max_nodes
sampling_spec?
```

Returns a time-aware evidence graph across state boundaries.

### 3.12 `wave_value`

Inputs:

```text
wave_id
signals[]
times[]
format?
```

The output value is a typed union. Default time semantics are
`value_at_or_before`.

### 3.13 `wave_changes`

Inputs:

```text
wave_id
signals[]
start
end
direction
max_changes
cursor?
```

Pagination cursors are bound to context, wave generation, signal, direction,
and time window.

### 3.14 `wave_compute`

Operations:

- sample
- find
- statistics
- compare
- first_divergence
- period
- pulse
- xz
- evaluate_window
- extract_events
- match_transactions

Inputs include `operation`, signals, wave IDs, time/window, expression IR or
operation-specific structured specs, and limits.

Protocol-specific names and conclusions are forbidden in the core operation
catalog.

### 3.15 `source_context`

Inputs:

```text
reference
before_lines
after_lines
max_lines
max_chars
include_preprocessor? (default false)
```

The reference MUST resolve to a source location in the current design context.
Arbitrary paths are not accepted.

`include_preprocessor=true` opts into Text NPI macro/include evidence. It is
lazy because initializing Text metadata can be expensive for large precompiled
design databases.

### 3.16 `assertion_structure`

Inputs:

```text
reference
max_source_lines? (default 200)
max_chars? (default 65536)
include_preprocessor? (default true)
allow_current_changed_source? (default false)
```

`reference` MUST be a current-generation language `ObjectRef` whose runtime NPI
type is `npiAssert`. Arbitrary source paths and caller-supplied assertion text
are not accepted.

The backend MUST run a context-local capability probe for assertion discovery
and source anchoring. Probe status is exposed under
`catalog(kind=backend_capabilities)`. An unavailable probe MUST produce
`unsupported_capability`; version strings alone MUST NOT imply support.

The response contains:

```text
anchor
source_evidence
property_anchor?
property_source_evidence?
npi_cross_reference
structure
truncated
```

`structure.fidelity` independently records:

```text
syntax: exact|unsupported
temporal: exact|unsupported
dependencies: exact|unresolved|opaque|unavailable
```

The supported V1 subset is explicit `posedge` or `negedge` clocking,
`disable iff`, `|->`, `|=>`, fixed/ranged `##`, and structural records for
`$past`, `$rose`, `$fell`, and `$stable`.

Ranged delays MUST remain symbolic cycle windows. Unsupported sequence
constructs MUST NOT produce partial temporal steps. Macro leaves may remain
`opaque`, but their internal identifiers MUST NOT be guessed.

The tool emits structural evidence and sampling requirements only. It MUST NOT
claim assertion pass/fail, vacuity, design quality, root cause, or a repair.

### 3.17 `mapping`

Actions:

- validate
- resolve
- explain

Supports same-context automatic fixed rules and cross-context explicit mapping
profiles.

### 3.18 `artifact`

Actions:

- export
- status
- list
- delete
- cancel

Large exports and long jobs return a `job_id`. Artifact writes are restricted
to the configured artifact root.

## 4. Development Tool Inventory

These tools are registered only when development tools are enabled.

### 4.1 `admin_doctor`

Checks environment, Verdi/Python NPI import, dynamic libraries, launcher,
worker transport, stdout isolation, path mapping, DB access, and FSDB access.

### 4.2 `admin_metrics`

Actions:

- snapshot
- reset
- export
- compare

Supports filters by tool, operation, context, request, and time range.

### 4.3 `admin_trace`

Actions:

- configure
- get
- list
- export

Detailed NPI spans MUST be explicitly enabled or sampled.

### 4.4 `admin_logs`

Reads bounded structured supervisor, worker, and native-output ring buffers.
Supports filtering and cursor pagination. It is not a generic file reader.

### 4.5 `admin_benchmark`

Runs versioned benchmark cases, records environment and fixture fingerprints,
and optionally compares against a baseline artifact.

### 4.6 `admin_selftest`

Runs transport, worker, fake backend, real NPI, design, waveform, mapping, and
semantic fixture tests. Missing environment dependencies MUST be reported as
unavailable, not passed.

## 5. Time Types

Input:

```json
{"value": "12.5", "unit": "ns"}
```

Output:

```json
{
  "ticks": "12500",
  "unit": "ps",
  "raw_ticks": "12500",
  "raw_scale_unit": "ps"
}
```

Allowed units MUST include `s`, `ms`, `us`, `ns`, `ps`, and `fs`.

## 6. Value Types

Logic:

```json
{
  "kind": "logic",
  "width": 4,
  "encoding": "bin",
  "value": "10xz",
  "signed": false
}
```

Real:

```json
{"kind": "real", "value": "1.250"}
```

String:

```json
{"kind": "string", "value": "IDLE"}
```

Enum:

```json
{"kind": "enum", "literal": "3'b010", "symbol": "STATE_RUN"}
```

## 7. Error Codes

### 7.1 Request and Schema

- `invalid_request`
- `unsupported_api_version` (unverified release, Python NPI import failure, or
  missing required NPI symbols)
- `unsupported_operation`
- `invalid_filter`
- `invalid_expression`
- `limit_exceeded`

### 7.2 Context and Worker

- `context_not_found`
- `context_not_active`
- `active_context_limit_reached`
- `worker_lost`
- `worker_busy`
- `soft_timeout`
- `hard_timeout`
- `resource_changed`
- `resource_not_attached`

### 7.3 Object and Capability

- `object_not_found`
- `ambiguous_name`
- `stale_object_id`
- `object_handle_limit_reached`
- `property_not_supported`
- `relation_not_supported`
- `unsupported_capability`
- `unsupported_object`

### 7.4 Waveform and Mapping

- `wave_not_found`
- `signal_not_dumped`
- `time_out_of_range`
- `value_not_representable`
- `cursor_expired`
- `cursor_mismatch`
- `ambiguous_mapping`
- `mapping_not_found`
- `temporal_evidence_incomplete`
- `sequential_evidence_incomplete`

### 7.5 Source and Artifact

- `source_unavailable`
- `source_changed`
- `source_outside_allowed_roots`
- `path_not_allowed`
- `artifact_not_found`
- `artifact_write_failed`
- `job_not_found`
- `job_cancelled`

Errors MAY include bounded diagnostics, but raw Python exceptions and unlimited
native output MUST NOT be the public error contract.

## 8. Truncation

Any incomplete result MUST include:

```json
{
  "truncated": true,
  "termination_reason": "node_limit",
  "scanned": 10000,
  "returned": 1000,
  "next_cursor": "..."
}
```

If a total was not fully calculated, it MUST be omitted or explicitly marked as
a lower bound.

## 9. Mapping Profile

Allowed deterministic rule forms:

- exact pair
- prefix replacement
- separator normalization
- bounded regex capture and replace
- explicit bit mapping

Each result MUST record the selected rule. More than one valid output is an
ambiguity error.

## 10. API Evolution

- Main semantic version is `stb.v1`.
- Compatible catalog additions do not require `stb.v2`.
- Removing, renaming, or changing semantics requires a new major API version.
- Expression semantics are independently versioned, initially `stb.expr.v1`.
