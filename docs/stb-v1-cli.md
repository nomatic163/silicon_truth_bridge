# STB v1 CLI Contract

The STB CLI is the human and automation interface to the same tool dispatcher
used by MCP. It must not implement separate backend behavior.

## Commands

The installed command is `stb`.

### Interactive

```bash
stb --backend verdi shell
```

The shell owns one supervisor lifecycle so opened design databases and
waveforms remain active across commands.

### Batch

```bash
stb batch workflow.jsonl
```

Each line is one strict tool request. All lines execute within one supervisor
lifecycle. Batch mode exits nonzero if a request fails or the workflow ends
with active resources that cannot be closed cleanly.

### One-Shot

```bash
stb --backend verdi call object_query --request query.json
```

One-shot mode is intended for context-independent administration and compact
workflows. Repeated design operations should use shell or batch mode to avoid
reloading large databases.

## Tool Coverage

All 17 core tools are available through:

```bash
stb call <tool-name> --request <file-or-stdin>
```

When development tools are enabled, the same entry point exposes all six
development tools.

Deferred ergonomic command groups may include:

```text
stb context
stb wave
stb object
stb connectivity
stb trace
stb waveform
stb source
stb mapping
stb artifact
stb doctor
stb metrics
stb logs
stb benchmark
stb selftest
```

Wrappers only construct validated tool requests. They cannot change semantics
or bypass limits.

## Input and Output

- JSON is the canonical input and output representation.
- JSONL is used for batch workflows and streaming output.
- Human-readable tables are summaries and never replace the JSON result.
- Requests may be read from a file or standard input.
- Responses may be written to standard output or an explicit output file.
- Source text and large waveform payloads are never printed unless the request
  explicitly asks for them.

Implemented presentation and debugging options:

```text
--pretty
--receipt-only
--save-request
--save-response
--output-file
```

Deferred CLI presentation and debugging wrappers:

```text
--output json|jsonl|table
--trace
--metrics
--timeout
```

## Schema and Replay

```bash
stb schema <tool>
stb catalog ...
stb replay request.json
```

Replay uses the original strict request schema. Resource fingerprints and
generations remain subject to normal validation.

## Exit Status

Current CLI exit codes distinguish:

- `0`: success
- `2`: failed request, invalid request, unavailable/changed resource, worker failure, timeout, or CLI transport failure
- `3`: partial result

The JSON response remains the authoritative error contract.

## Non-Goals

V1 does not provide:

- a background HTTP daemon
- a Unix-socket service
- arbitrary shell or Python execution
- direct access to internal worker commands
- a separate CLI-only cache or context model
