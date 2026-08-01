# STB v1 Verification and Delivery Plan

## 1. Test Layers

### 1.1 Static Contract Checks

- JSON schema validation
- catalog uniqueness and type validation
- stable error-code validation
- example request/response validation
- documentation/tool inventory consistency

### 1.2 Hermetic Tests

Use `FakeBackend` without Verdi or license dependencies.

Required coverage:

- all 17 core tool schemas
- all six development tools
- context and wave state machines
- batching and partial success
- limits, pagination, and cursors
- stale generations
- artifact and async job lifecycle
- timeout and worker-loss translation
- metrics, logs, and trace correlation
- CLI one-shot, shell, batch, schema, and replay behavior

### 1.3 Real SP1 Integration Tests

Use Verdi `V-2023.12-SP1`, a licensed environment, generated design DB
fixtures, and FSDB fixtures.

The test harness MUST fail or report environment unavailable when dependencies
are absent. It MUST NOT silently switch to fake behavior.

Each additional Verdi release requires its own real test row recording:

- release and service pack
- worker Python version
- NPI symbol probe result
- design-only, waveform-only, and combined test result
- Codex and Claude Code MCP smoke result

Passing only the symbol probe keeps the release `unverified`.

## 2. RTL Fixture Matrix

| Fixture | Required Evidence |
|---|---|
| hierarchy | module instances, ports, nets, source locations |
| `.svh/.vh` includes | include chain, macro context, source mapping |
| nested macros | definition, invocation, arguments, bounded expansion |
| parameter override | effective value and elaboration evidence |
| generate if/case/for | selected and excluded elaboration branches |
| continuous assign | direct and recursive drivers |
| ternary and case | structural and runtime branch selection |
| concat/slice | exact bit mapping |
| multiple drivers | all candidates and resolution evidence |
| tri-state | enable, Z contribution, effective resolution |
| protected/black-box | explicit boundary and no invented source |
| always_comb | assignment and control dependencies |
| always_ff | event control, reset, enable, sampled RHS |
| latch | transparent-window value origin |
| memory | indexed write-origin evidence |
| force/release/deposit | override separated from RTL driver |
| X/Z control | feasible branches and indeterminate selection |
| combinational loop | loop detection |

## 3. Waveform Fixture Matrix

| Fixture | Required Evidence |
|---|---|
| scalar four-state | exact `0/1/x/z` |
| wide vector | width, slicing, output limits |
| real/string/enum | typed value union |
| multiple timescales | exact conversion without float |
| duplicate timestamp changes | temporal resolution reporting |
| FSDB with sequence numbers | `exact_sequence` behavior |
| FSDB without sequence numbers | `time_bucket` behavior |
| undumped signal | `signal_not_dumped` |
| changed FSDB path | `resource_changed`, then reload |
| long transition stream | cursor pagination and cancellation |
| two FSDBs | explicit wave selection and comparison |
| pass/fail divergence | first divergence and active-path comparison |

## 4. Failure and Lifecycle Matrix

| Scenario | Expected Result |
|---|---|
| invalid schema | global `invalid_request` |
| one bad batch item | partial success |
| all batch items invalid | failed batch summary |
| context hard limit | no eviction; explicit limit error |
| stale object ID | `stale_object_id` |
| stale cursor | cursor error |
| source file changed | `source_changed` |
| soft timeout | bounded partial result where safe |
| native hard timeout | worker terminated and context crashed |
| worker crash | supervisor remains responsive |
| context reload | generation increment and cache reset |
| wave reload | wave generation increment and cursor reset |
| MCP shutdown | child cleanup and no orphan worker |

## 5. Performance Benchmarks

Required cases:

1. context open and design load
2. name resolution cold and warm
3. `object_get` batches of increasing size
4. direct driver/load batches
5. bounded trace graph
6. batch waveform values
7. transition scans at fixed data volumes
8. expression evaluation over a fixed cycle count
9. design-to-wave mapping cold and warm
10. active-driver simple and branching cases
11. value-origin across fixed cycle depths
12. serialization and MCP transport overhead

Each run MUST record:

- direct NPI, worker, and MCP measurements
- median and p95
- process RSS and CPU
- NPI calls and NPI duration
- Python processing duration
- serialization duration
- input and output sizes
- objects or transitions scanned
- cache state
- fixture and environment fingerprint
- STB build and source revision

The default regression warning threshold SHOULD be 10%, configurable per case.
Correctness failures are always hard failures.

## 6. Delivery Phases

### Phase 0: Contract Foundation

- project packaging and configuration
- schemas and generated Python models
- stable errors and receipts
- fake backend
- local worker protocol
- core observability primitives

Exit criteria: hermetic context lifecycle and schema tests pass.

### Phase 1: Resource and Waveform Foundation

- local launcher and NPI initialization
- context/wave lifecycle and generations
- FSDB metadata, hierarchy, value, and changes
- exact time/value contracts
- cursor pagination and waveform mapping

Exit criteria: real SP1 waveform fixture suite passes.

### Phase 2: Generic Design Object Layer

- Netlist and Language object references
- property/relation catalogs
- resolve, get, query, and traverse
- handle store and source context

Exit criteria: generic API and source/header fixtures pass.

### Phase 3: Connectivity and Trace

- direct driver/load
- evidence graph
- fanin/fanout/path traversal
- bit mapping and semantic classification
- trace limits and artifact export

Exit criteria: connectivity fixture matrix passes.

### Phase 4: Cross-Model Semantics

- active-driver branch pipeline
- macro/generate/runtime/resolution layers
- force/release/deposit evidence
- temporal resolution reporting
- value-origin for flop, latch, and memory

Exit criteria: active-driver and value-origin golden tests pass.

### Phase 5: Mechanical Wave Computation

- expression operator catalog
- scalar/temporal/transaction IR
- sample, find, statistics, compare, pulse, period, X/Z
- event extraction and transaction matching

Exit criteria: expression and computation fixtures pass.

### Phase 6: Hardening and Optimization

- cancellation and hard-timeout recovery
- async jobs and artifact lifecycle
- allowed-root enforcement
- benchmark baselines
- measured cache tuning
- optional native fast paths only when justified

Exit criteria: lifecycle, security, and performance gates pass.

## 7. Documentation Gates

Before v1 release:

- tool inventory in all documents must match runtime registration
- every example must validate against generated schemas
- every error code must have at least one test
- catalog support status must match real adapter behavior
- no claim of VHDL/SystemC semantic support may exceed fixture coverage

## 8. Definition of Done

v1 is done only when:

- all required phases meet exit criteria
- fake and real test layers pass in their required environments
- benchmark baseline artifacts are published
- no silent fallback, silent truncation, or implicit resource reload remains
- Codex can complete representative design-only, wave-only, and combined
  evidence workflows using only the registered v1 tools
- Claude Code can complete a representative real waveform workflow using the
  same registered typed tools
- the same representative workflows can be reproduced through `stb batch`

The MCP protocol and schemas remain client-neutral.
