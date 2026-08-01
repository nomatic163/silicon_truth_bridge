# STB v1 Architecture

## 1. System Shape

```text
Codex / Claude Code
        |
        | MCP stdio
        v
MCP Supervisor
  - schemas and validation
  - context registry
  - worker launchers
  - routing and serialization
  - metrics, logs, artifacts, jobs
        |
        | private JSONL protocol
        v
NPI Worker per active evidence context
  - one npisys.init()
  - zero or one loaded design DB
  - zero or more FileHandle objects
  - serialized NPI execution
  - generation-scoped caches
```

The supervisor MUST never import or initialize NPI in the MCP stdio process.
Native crashes and stdout pollution must remain confined to workers.

## 2. Main Components

### 2.1 MCP Supervisor

Responsibilities:

- register core and optional development tools
- validate `stb.v1` requests
- enforce allowed roots and hard limits
- manage evidence contexts and launchers
- serialize requests per worker
- route async jobs
- maintain structured logs and aggregate metrics
- convert worker loss into stable errors

### 2.2 Worker Launcher

Required interface:

```text
start(context_spec) -> WorkerChannel
terminate(worker_id, reason)
status(worker_id)
collect_process_metrics(worker_id)
```

The local launcher MUST use an argv array and dedicated stdin/stdout/stderr
channels. Future LSF/SSH launchers MUST preserve the same worker protocol.

### 2.3 NPI Worker

Responsibilities:

- call `npisys.init()` exactly once
- optionally call `npisys.load_design()` once
- open and close named waveform handles
- host object/property/relation adapters
- execute trace and waveform operations
- own all NPI/Python handles
- emit structured response and metric records
- call `npisys.end()` during normal shutdown

### 2.4 Catalog Registry

The catalog is the compatibility boundary between stable STB semantics and
version-specific NPI methods.

Catalog entries MUST declare:

- stable name
- model and object types
- input and output type
- source NPI method or adapter
- cost class
- support status
- Verdi version constraints
- mutability or exclusion reason

### 2.5 Assertion Structure Adapter

The assertion adapter is Verdi-first and fail-closed:

```text
language npiAssert ObjectRef
  -> runtime capability probe
  -> NPI source and named-property anchors
  -> bounded source snapshot
  -> supported-subset parser
  -> NPI identifier cross-reference
  -> structured evidence
```

The parser does not replace Verdi object discovery and does not accept arbitrary
files. NPI relationships establish identity and source location; the bounded
parser handles only the reviewed syntax subset. Unsupported temporal semantics
preserve raw evidence but produce no partial timeline.

Expression leaves remain raw source. Identifier tokens become signal evidence
only after exact NPI resolution. Macro bodies, local-variable classification,
bit semantics, pass/fail evaluation, and narrative conclusions are outside this
adapter.

### 2.6 Artifact and Job Manager

Artifacts MUST live below a controlled artifact root and include a metadata
sidecar with request, receipt, fingerprint, schema version, and generation.

Long jobs MUST have explicit state:

```text
queued -> running -> completed
                  -> failed
                  -> cancelled
```

## 3. State Machines

### 3.1 Evidence Context

```text
registered
    |
    | open
    v
opening -> active -> closing -> closed
             |
             | reload
             v
          reloading -> active
             |
             | crash / hard timeout
             v
           crashed
```

Only `active` contexts may execute NPI queries. A `crashed` context requires an
explicit reload or close.

### 3.2 Waveform Attachment

```text
detached -> attaching -> active -> reloading -> active
                          |
                          -> changed
                          -> failed
                          -> detached
```

Queries against `changed` waveforms MUST return `resource_changed`.

### 3.3 Request

```text
received -> validated -> queued -> running -> completed
                    |                |
                    -> rejected      -> partial
                                     -> timed_out
                                     -> worker_lost
                                     -> failed
```

## 4. Reference Model

```json
{
  "model": "netlist|language|waveform",
  "npi_type": "DECL_NET",
  "full_name": "top.u_core.req",
  "object_id": null,
  "context_id": "rtl",
  "worker_generation": 3
}
```

Rules:

- `full_name` is primary for named objects.
- `object_id` is required for anonymous Language objects.
- both fields MAY be present
- a reference from another context or generation is invalid
- graph-local `node_id` is not an object reference

The object handle store MUST:

- allocate only for returned anonymous objects
- enforce `max_object_handles`
- avoid automatic eviction
- support explicit release
- clear on reload or close

## 5. Object Projection

```json
{
  "ref": {},
  "name": "req",
  "semantic_class": "combinational_net",
  "source": {
    "file": "${PROJECT_ROOT}/rtl/core.sv",
    "begin_line": 120,
    "end_line": 120
  }
}
```

`ObjectDetail` extends this summary with requested properties and supported
relations. Unrequested properties MUST NOT be fetched merely for convenience.

## 6. Evidence Graph

```json
{
  "roots": ["g1"],
  "nodes": [
    {"node_id": "g1", "origin": "npi", "ref": {}},
    {
      "node_id": "g2",
      "origin": "derived",
      "kind": "bit_mapping",
      "derivation": {"rule": "actual_name_list"}
    }
  ],
  "edges": [
    {"from": "g2", "to": "g1", "relation": "drives"}
  ],
  "terminals": [
    {"node_id": "g2", "reason": "sequential_boundary"}
  ]
}
```

Typed edges SHOULD include:

- contains
- connects
- drives
- loads
- data_dependency
- control_dependency
- port_connection
- bit_mapping
- active_branch
- feasible_branch
- sampled_from
- overridden_by

## 7. Active-Driver Pipeline

```text
resolve target
-> build structural driver graph
-> annotate compile-time selection
-> annotate elaboration-time selection
-> map controls to waveform signals
-> evaluate runtime branches
-> evaluate bit and multi-driver resolution
-> apply recorded simulation overrides
-> report temporal resolution and missing evidence
```

The pipeline MUST retain evidence before pruning. A selected active branch is an
annotation on the structural graph, not a replacement for it.

## 8. Value-Origin Pipeline

```text
resolve state element
-> classify flop/latch/memory/unknown
-> extract event controls and write conditions
-> locate latest effective update
-> sample RHS and controls
-> add sampled_from edge
-> continue with bounded earlier time
```

Every hop MUST consume from a traversal budget. Missing or ambiguous scheduling
data MUST terminate or branch the graph rather than select a guessed path.

## 9. Waveform Execution

The waveform adapter SHOULD prefer native batch APIs where available.

Execution plans SHOULD:

- resolve all signal handles once
- group compatible signals
- normalize time once per waveform
- share clock edge vectors within one request
- release temporary transition buffers after the request
- avoid total-count scans unless requested

## 10. Expression IR

```json
{
  "expr_version": "stb.expr.v1",
  "root": {
    "op": "logic.and",
    "args": [
      {"op": "logic.eq", "args": [{"signal": "top.valid"}, {"literal": "1'b1"}]},
      {"op": "logic.is_known", "args": [{"signal": "top.data"}]}
    ]
  }
}
```

The expression compiler MUST:

1. validate the operator catalog
2. resolve and type signals
3. enforce width and four-state semantics
4. normalize the AST
5. estimate cost and enforce limits
6. compile or retrieve an execution plan

## 11. Caching

```text
context generation
  - object name cache
  - object property cache
  - direct relation cache
  - semantic classification cache

wave generation
  - waveform signal cache
  - design-to-wave mapping cache
  - negative mapping cache

process
  - expression plan cache keyed by catalog version and normalized AST
```

Reload MUST use whole-generation invalidation. v1 MUST NOT implement complex
fine-grained cache invalidation.

## 12. Fingerprints

A lightweight resource fingerprint SHOULD include:

- canonical path
- resource kind
- size
- modification time
- selected directory metadata for DB directories
- load specification fingerprint

Full content hashing of large DBs and FSDBs MUST NOT be the default.

## 13. Receipts

Every successful or partial evidence response MUST contain one compact receipt:

```json
{
  "api_version": "stb.v1",
  "request_id": "req-123",
  "context_id": "rtl",
  "worker_generation": 3,
  "design_fingerprint": "meta:...",
  "wave_id": "fail",
  "wave_generation": 2,
  "verdi_version": "V-2023.12-SP1",
  "verdi_compatibility": "verified",
  "backend": "python_npi",
  "limits": {"truncated": false},
  "metrics": {
    "duration_ms": 83,
    "npi_calls": 17,
    "cache_hits": 12
  }
}
```

## 14. Observability

Low-overhead counters MUST remain enabled in development mode. Detailed spans
MUST be opt-in or sampled.

Required metric dimensions include:

- tool and operation
- context and backend
- success, partial, error code
- latency and queue time
- NPI calls and NPI duration
- objects and transitions scanned
- cache hits and misses
- request and response bytes
- truncation and timeout
- worker lifecycle events

Supervisor and worker logs MUST be structured and correlated by `request_id`.

## 15. Backend Strategy

v1 uses:

- `PythonNpiBackend` for real SP1 access
- `FakeBackend` for hermetic contract tests
- optional `NativeFastPathBackend` only for measured hotspots

All backends MUST implement identical STB schemas and error behavior.
