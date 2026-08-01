from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from stb.assertions import parse_assertion_source, resolve_structure_dependencies
from stb.backends.base import Backend
from stb.cursors import CursorRegistry
from stb.errors import StbError
from stb.query import QUERY_OPERATORS, evaluate_where
from stb.timeutil import parse_time, raw_time_point


class FakeBackend(Backend):
    name = "fake"

    def __init__(
        self,
        context_id: str,
        generation: int = 1,
        wave_specs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.context_id = context_id
        self.generation = generation
        self._cursors = CursorRegistry()
        self._mapping_cache: dict[str, dict[str, Any]] = {}
        self.waves: dict[str, dict[str, Any]] = {}
        self.objects = {
            "top": {
                "model": "netlist",
                "npi_type": "INST",
                "name": "top",
                "full_name": "top",
                "semantic_class": "module_instance",
                "width": None,
                "children": ["top.u_core"],
            },
            "top.u_core": {
                "model": "netlist",
                "npi_type": "INST",
                "name": "u_core",
                "full_name": "top.u_core",
                "semantic_class": "module_instance",
                "width": None,
                "children": ["top.u_core.req", "top.u_core.data"],
            },
            "top.u_core.req": {
                "model": "netlist",
                "npi_type": "DECL_NET",
                "name": "req",
                "full_name": "top.u_core.req",
                "semantic_class": "combinational_net",
                "width": 1,
                "children": [],
            },
            "top.u_core.data": {
                "model": "netlist",
                "npi_type": "DECL_NET",
                "name": "data",
                "full_name": "top.u_core.data",
                "semantic_class": "register",
                "width": 32,
                "children": [],
            },
        }
        self.assertions = {
            "top.a_req_to_data": {
                "model": "language",
                "npi_type": "npiAssert",
                "name": "a_req_to_data",
                "full_name": "top.a_req_to_data",
                "semantic_class": "concurrent_assertion",
                "source": {
                    "file": "fake/assertions.sv",
                    "begin_line": 4,
                    "end_line": 7,
                    "include_chain": [],
                },
                "text": (
                    "a_req_to_data: assert property (\n"
                    "  @(posedge top.u_core.req)\n"
                    "    top.u_core.req |-> ##[1:3] top.u_core.data\n"
                    ");"
                ),
            }
        }
        for spec in wave_specs or []:
            self._attach_wave(spec["wave_id"], spec["path"])

    def _summary(self, obj: dict[str, Any]) -> dict[str, Any]:
        return {
            "ref": {
                "model": obj["model"],
                "context_id": self.context_id,
                "worker_generation": self.generation,
                "npi_type": obj["npi_type"],
                "full_name": obj["full_name"],
            },
            "name": obj["name"],
            "semantic_class": obj["semantic_class"],
            "classification_rule": "fake_fixture",
        }

    def _assertion_summary(self, obj: dict[str, Any]) -> dict[str, Any]:
        return {
            "ref": {
                "model": "language",
                "context_id": self.context_id,
                "worker_generation": self.generation,
                "npi_type": obj["npi_type"],
                "full_name": obj["full_name"],
                "object_id": f"fake-lang-{obj['name']}",
            },
            "name": obj["name"],
            "semantic_class": obj["semantic_class"],
            "classification_rule": "fake_fixture",
            "source": obj["source"],
        }

    def catalog(self, kind: str, filters: dict[str, Any]) -> Any:
        catalogs = {
            "models": ["netlist", "language", "waveform"],
            "object_types": ["INST", "PORT", "INSTPORT", "DECL_NET", "npiAssert"],
            "semantic_classes": [
                "module_instance",
                "register",
                "combinational_net",
                "concurrent_assertion",
            ],
            "properties": ["name", "full_name", "width", "semantic_class"],
            "relations": ["children"],
            "backend_capabilities": {
                "backend": self.name,
                "real_npi": False,
                "assertion_structure": {
                    "status": "available",
                    "probe": "fake_fixture",
                },
            },
            "wave_operations": [
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
            ],
            "operators": [
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
            ],
            "query_operators": sorted(QUERY_OPERATORS),
            "limits": {
                "default_query_limit": 100,
                "default_trace_nodes": 1000,
                "default_wave_changes": 1000,
                "hard_expression_nodes": 1000,
                "hard_evaluation_points": 100_000,
            },
        }
        if kind not in catalogs:
            raise StbError("unsupported_capability", f"unsupported catalog kind: {kind}")
        return catalogs[kind]

    def object_resolve(self, args: dict[str, Any]) -> Any:
        if args.get("model") == "waveform":
            wave_id, wave = self._wave(args.get("wave_id"))
            name = args["name"]
            if name not in wave["signals"]:
                raise StbError("object_not_found", f"waveform object not found: {name}")
            return self._wave_summary(wave_id, name)
        name = args["name"]
        if args.get("model") == "language":
            assertion = self.assertions.get(name)
            if assertion is None:
                raise StbError("object_not_found", f"language object not found: {name}")
            return self._assertion_summary(assertion)
        obj = self.objects.get(name)
        if obj is None:
            raise StbError("object_not_found", f"object not found: {name}")
        return self._summary(obj)

    def object_get(self, args: dict[str, Any]) -> Any:
        result = []
        for ref in args["references"]:
            try:
                self.validate_ref(ref)
                name = ref.get("full_name")
                if ref.get("model") == "language":
                    assertion = self.assertions.get(name)
                    if assertion is None:
                        raise StbError(
                            "object_not_found",
                            f"language object not found: {name}",
                        )
                    props = {
                        key: assertion.get(key)
                        for key in args.get("properties", [])
                    }
                    result.append(
                        {
                            "ok": True,
                            "summary": self._assertion_summary(assertion),
                            "properties": props,
                        }
                    )
                    continue
                obj = self.objects.get(name)
                if obj is None:
                    raise StbError("object_not_found", f"object not found: {name}")
                props = {key: obj.get(key) for key in args.get("properties", [])}
                result.append(
                    {"ok": True, "summary": self._summary(obj), "properties": props}
                )
            except StbError as exc:
                result.append({"ok": False, "error_code": exc.code})
        return result

    def object_query(self, args: dict[str, Any]) -> Any:
        if args.get("model") == "waveform":
            wave_id, wave = self._wave(args.get("wave_id"))
            scope = args.get("scope") or ""
            matches = [
                self._wave_summary(wave_id, name)
                for name in sorted(wave["signals"])
                if not scope or name.startswith(scope + ".") or name == scope
            ]
            limit = int(args.get("limit", 100))
            return {"objects": matches[:limit], "truncated": len(matches) > limit}
        if args.get("model") == "language":
            scope = args.get("scope") or ""
            npi_types = set(args.get("npi_types", []))
            semantic = set(args.get("semantic_classes", []))
            matches = []
            for obj in self.assertions.values():
                if scope and not obj["full_name"].startswith(scope):
                    continue
                if npi_types and obj["npi_type"] not in npi_types:
                    continue
                if semantic and obj["semantic_class"] not in semantic:
                    continue
                matches.append(self._assertion_summary(obj))
            limit = int(args.get("limit", 100))
            return {"objects": matches[:limit], "truncated": len(matches) > limit}
        scope = args.get("scope", "")
        npi_types = set(args.get("npi_types", []))
        semantic = set(args.get("semantic_classes", []))
        matches = []
        for obj in self.objects.values():
            if scope and not obj["full_name"].startswith(scope):
                continue
            if npi_types and obj["npi_type"] not in npi_types:
                continue
            if semantic and obj["semantic_class"] not in semantic:
                continue
            if not evaluate_where(
                args.get("where"),
                lambda prop, current=obj: self._query_property(current, prop),
            ):
                continue
            matches.append(self._summary(obj))
        limit = args.get("limit", 100)
        return {"objects": matches[:limit], "truncated": len(matches) > limit}

    def _query_property(self, obj: dict[str, Any], prop: str) -> Any:
        if prop not in {"name", "full_name", "width", "semantic_class", "npi_type"}:
            raise StbError("invalid_request", f"property is not queryable: {prop}")
        return obj.get(prop)

    def object_traverse(self, args: dict[str, Any]) -> Any:
        if args.get("relation") != "children":
            raise StbError("relation_not_supported", "fake backend supports only children")
        depth = int(args.get("depth", 1))
        maximum = int(args.get("max_nodes", 1000))
        key = {
            "operation": "object_traverse",
            "context_id": self.context_id,
            "worker_generation": self.generation,
            "roots": args["roots"],
            "relation": args["relation"],
            "depth": depth,
            "filters": args.get("filters"),
        }
        state = self._cursors.get(args.get("cursor"))
        if state:
            if state["key"] != key:
                raise StbError("cursor_mismatch", "cursor does not match traversal")
            queue = list(state["queue"])
            pending = list(state["pending"])
            seen = set(state["seen"])
            scanned = int(state["scanned"])
        else:
            for ref in args["roots"]:
                self.validate_ref(ref)
            queue = [
                {"full_name": ref["full_name"], "level": 0}
                for ref in args["roots"]
            ]
            pending = []
            seen = {ref["full_name"] for ref in args["roots"]}
            scanned = 0
        result = []
        termination_reason = None
        while len(result) < maximum and (pending or queue):
            if self.soft_timed_out():
                termination_reason = "soft_timeout"
                break
            while pending and len(result) < maximum:
                if self.soft_timed_out():
                    termination_reason = "soft_timeout"
                    break
                result.append(pending.pop(0))
            if termination_reason:
                break
            if len(result) >= maximum or not queue:
                break
            current = queue.pop(0)
            if current["level"] >= depth:
                continue
            obj = self.objects.get(current["full_name"])
            if not obj:
                continue
            scanned += len(obj["children"])
            for child in obj["children"]:
                if child in seen:
                    continue
                seen.add(child)
                pending.append(self._summary(self.objects[child]))
                queue.append({"full_name": child, "level": current["level"] + 1})
        truncated = bool(pending or queue)
        cursor = (
            self._cursors.issue(
                {
                    "key": key,
                    "queue": queue,
                    "pending": pending,
                    "seen": sorted(seen),
                    "scanned": scanned,
                }
            )
            if truncated
            else None
        )
        return {
            "objects": result,
            "truncated": truncated,
            "scanned": scanned,
            "returned": len(result),
            "termination_reason": termination_reason
            or ("node_limit" if truncated else None),
            "next_cursor": cursor,
        }

    def release_objects(self, object_ids: list[str]) -> Any:
        return {"released": [], "missing": list(object_ids)}

    def _attach_wave(self, wave_id: str, path: str) -> dict[str, Any]:
        if wave_id in self.waves:
            raise StbError("invalid_request", f"wave already attached: {wave_id}")
        req_end = 12_000_000 if "divergent" in path else 17_000_000
        self.waves[wave_id] = {
            "path": path,
            "generation": 1,
            "signals": {
                "top.clk": [(0, "0"), (5_000_000, "1"), (10_000_000, "0"),
                            (15_000_000, "1"), (20_000_000, "0")],
                "top.req": [(0, "0"), (7_000_000, "1"), (req_end, "0")],
                "top.data": [(0, "0" * 32), (10_000_000, "1" * 32)],
            },
        }
        self._cursors.clear()
        self._mapping_cache.clear()
        return self._wave_info(wave_id)

    def _wave(self, wave_id: str | None) -> tuple[str, dict[str, Any]]:
        if wave_id is None:
            if len(self.waves) != 1:
                raise StbError("invalid_request", "wave_id is required")
            wave_id = next(iter(self.waves))
        wave = self.waves.get(wave_id)
        if wave is None:
            raise StbError("wave_not_found", f"wave not found: {wave_id}")
        return wave_id, wave

    def _wave_info(self, wave_id: str) -> dict[str, Any]:
        wave = self.waves[wave_id]
        return {
            "wave_id": wave_id,
            "path": wave["path"],
            "fingerprint": f"fake:{wave_id}",
            "wave_generation": wave["generation"],
            "name": wave["path"],
            "min_time": "0",
            "max_time": "20000000",
            "scale_unit": "1fs",
            "version": "fake.v1",
            "is_completed": True,
            "has_glitch": False,
            "has_seq_num": True,
            "has_reason_code": False,
            "has_force_tag": False,
        }

    def _wave_summary(self, wave_id: str, name: str) -> dict[str, Any]:
        width = len(self.waves[wave_id]["signals"][name][0][1])
        return {
            "ref": {
                "model": "waveform",
                "context_id": self.context_id,
                "worker_generation": self.generation,
                "npi_type": "SIGNAL",
                "full_name": name,
            },
            "name": name.rsplit(".", 1)[-1],
            "semantic_class": "waveform_signal",
            "classification_rule": "fake_fixture",
            "description": f"width={width};wave_id={wave_id}",
        }

    def wave_generation(self, wave_id: str | None) -> int | None:
        try:
            return int(self._wave(wave_id)[1]["generation"])
        except StbError:
            return None

    def wave_manage(self, args: dict[str, Any]) -> Any:
        action = args["action"]
        wave_id = args.get("wave_id")
        if action == "attach":
            return self._attach_wave(wave_id, args["path"])
        if action == "list":
            return {"waves": [self._wave_info(name) for name in sorted(self.waves)]}
        if action == "status":
            resolved, _ = self._wave(wave_id)
            return self._wave_info(resolved)
        if action == "detach":
            resolved, _ = self._wave(wave_id)
            del self.waves[resolved]
            self._cursors.clear()
            self._mapping_cache.clear()
            return {"wave_id": resolved, "state": "detached"}
        if action == "reload":
            resolved, wave = self._wave(wave_id)
            path = args.get("path") or wave["path"]
            generation = int(wave["generation"]) + 1
            del self.waves[resolved]
            result = self._attach_wave(resolved, path)
            self.waves[resolved]["generation"] = generation
            result["wave_generation"] = generation
            self._mapping_cache.clear()
            return result
        raise StbError("unsupported_operation", f"unsupported wave action: {action}")

    def _raw_time(self, value: str) -> int:
        parsed = parse_time(value)
        if parsed.denominator != 1:
            raise StbError("invalid_request", "fake waveform requires integral fs")
        return parsed.numerator

    def _value_at(self, rows: list[tuple[int, str]], time_fs: int) -> str:
        value = rows[0][1]
        for change_time, changed in rows:
            if change_time > time_fs:
                break
            value = changed
        return value

    def wave_value(self, args: dict[str, Any]) -> Any:
        wave_id, wave = self._wave(args.get("wave_id"))
        values = []
        for time_spec in args["times"]:
            time_fs = self._raw_time(time_spec)
            if not 0 <= time_fs <= 20_000_000:
                raise StbError("time_out_of_range", f"time outside fake wave: {time_spec}")
            for signal in args["signals"]:
                rows = wave["signals"].get(signal)
                if rows is None:
                    values.append(
                        {"signal": signal, "time": raw_time_point(time_fs, "1fs"),
                         "ok": False, "error_code": "signal_not_dumped"}
                    )
                    continue
                value = self._value_at(rows, time_fs)
                values.append(
                    {
                        "signal": signal,
                        "time": raw_time_point(time_fs, "1fs"),
                        "ok": True,
                        "value": {
                            "kind": "logic",
                            "width": len(value),
                            "encoding": "bin",
                            "value": value,
                        },
                    }
                )
        return {"wave_id": wave_id, "values": values}

    def wave_changes(self, args: dict[str, Any]) -> Any:
        wave_id, wave = self._wave(args.get("wave_id"))
        start = self._raw_time(args["start"])
        end = self._raw_time(args["end"])
        direction = args.get("direction", "forward")
        maximum = int(args.get("max_changes", 1000))
        key = {
            "wave_id": wave_id,
            "wave_generation": wave["generation"],
            "signals": args["signals"],
            "start": start,
            "end": end,
            "direction": direction,
        }
        state = self._cursors.get(args.get("cursor"))
        positions: dict[str, dict[str, Any]] = {}
        if state:
            if state["key"] != key:
                raise StbError("cursor_mismatch", "cursor does not match request")
            if "positions" in state:
                positions = state["positions"]
            else:
                positions = {
                    signal: {"offset": int(state.get("offset", 0))}
                    for signal in args["signals"]
                }
        output = []
        has_more = False
        next_positions: dict[str, dict[str, Any]] = {}
        returned = 0
        for signal in args["signals"]:
            position = positions.get(signal) or {}
            if position.get("missing"):
                output.append(
                    {"signal": signal, "ok": False, "error_code": "signal_not_dumped"}
                )
                next_positions[signal] = {"missing": True}
                continue
            rows = wave["signals"].get(signal)
            if rows is None:
                output.append(
                    {"signal": signal, "ok": False, "error_code": "signal_not_dumped"}
                )
                next_positions[signal] = {"missing": True}
                continue
            if position.get("done"):
                selected = []
                page = []
                more = False
                next_positions[signal] = {"done": True}
            else:
                selected = [row for row in rows if start <= row[0] <= end]
                if direction == "backward":
                    selected.reverse()
                offset = int(position.get("offset", 0))
                page = selected[offset : offset + maximum]
                more = len(selected) > offset + maximum
                next_positions[signal] = (
                    {"offset": offset + maximum} if more else {"done": True}
                )
            has_more = has_more or more
            returned += len(page)
            output.append(
                {
                    "signal": signal,
                    "ok": True,
                    "changes": [
                        {
                            "time": raw_time_point(time_fs, "1fs"),
                            "value": {
                                "kind": "logic",
                                "width": len(value),
                                "encoding": "bin",
                                "value": value,
                            },
                        }
                        for time_fs, value in page
                    ],
                    "truncated": more,
                }
            )
        cursor = (
            self._cursors.issue({"key": key, "positions": next_positions})
            if has_more
            else None
        )
        return {
            "wave_id": wave_id,
            "signals": output,
            "direction": direction,
            "next_cursor": cursor,
            "truncated": has_more,
            "termination_reason": "transition_limit" if has_more else None,
            "returned": returned,
            "scanned": returned,
        }

    def connectivity_direct(self, args: dict[str, Any]) -> Any:
        items = []
        for signal in args["signals"]:
            if signal not in self.objects:
                items.append({"signal": signal, "ok": False, "error_code": "object_not_found"})
                continue
            related_name = "top.u_core.req" if args["kind"] == "driver" else "top.u_core.data"
            items.append(
                {
                    "signal": signal,
                    "ok": True,
                    "resolved": self._summary(self.objects[signal]),
                    "resolution_rule": "exact",
                    "objects": [self._summary(self.objects[related_name])],
                }
            )
        return {"kind": args["kind"], "items": items}

    def trace(self, args: dict[str, Any]) -> Any:
        nodes = []
        for index, root in enumerate(args["roots"]):
            if isinstance(root, dict):
                self.validate_ref(root)
            name = root.get("full_name") if isinstance(root, dict) else root
            if name not in self.objects:
                raise StbError("object_not_found", f"object not found: {name}")
            nodes.append(
                {"node_id": f"g{index + 1}", "origin": "npi", **self._summary(self.objects[name])}
            )
        return {
            "roots": [row["node_id"] for row in nodes],
            "nodes": nodes,
            "edges": [],
            "terminals": [{"node_id": row["node_id"], "reason": "fake_fixture"} for row in nodes],
            "limits": {"max_depth": args.get("max_depth", 20), "max_nodes": args.get("max_nodes", 1000), "truncated": False},
        }

    def _expr_signals(self, node: Any, output: set[str], count: list[int]) -> None:
        count[0] += 1
        if count[0] > 1000:
            raise StbError("limit_exceeded", "expression exceeds 1000 nodes")
        if not isinstance(node, dict):
            raise StbError("invalid_request", "expression node must be an object")
        if "signal" in node:
            output.add(str(node["signal"]))
            return
        if "literal" in node:
            return
        op = node.get("op")
        if op not in {
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
        }:
            raise StbError("unsupported_capability", f"unsupported expression operator: {op}")
        for child in node.get("args") or []:
            self._expr_signals(child, output, count)

    def _literal_bits(self, value: Any) -> str:
        text = str(value).strip().lower().replace("_", "")
        match = re.fullmatch(r"(\d+)'b([01xz]+)", text)
        if match:
            width = int(match.group(1))
            bits = match.group(2)
            return bits[-width:].rjust(width, "0")
        if re.fullmatch(r"[01xz]+", text):
            return text
        if text in {"true", "false"}:
            return "1" if text == "true" else "0"
        raise StbError("invalid_request", f"unsupported logic literal: {value}")

    def _logic_truth(self, bits: str) -> bool | None:
        if any(bit in bits for bit in "xz"):
            return None
        return any(bit == "1" for bit in bits)

    def _eval_expr(self, node: dict[str, Any], values: dict[str, str]) -> str:
        if "signal" in node:
            signal = str(node["signal"])
            if signal not in values:
                raise StbError("signal_not_dumped", f"signal not dumped: {signal}")
            return values[signal]
        if "literal" in node:
            return self._literal_bits(node["literal"])
        op = node["op"]
        args = [self._eval_expr(child, values) for child in node.get("args") or []]
        if op in {"logic.eq", "logic.ne"}:
            if len(args) != 2:
                raise StbError("invalid_request", f"{op} requires two arguments")
            result = "x" if any(any(bit in value for bit in "xz") for value in args) else (
                "1" if args[0] == args[1] else "0"
            )
            return (
                "1" if result == "0" else "0" if result == "1" else "x"
            ) if op == "logic.ne" else result
        if op in {"logic.and", "logic.or"}:
            truths = [self._logic_truth(value) for value in args]
            if op == "logic.and":
                return "0" if False in truths else "x" if None in truths else "1"
            return "1" if True in truths else "x" if None in truths else "0"
        if op == "logic.not":
            if len(args) != 1:
                raise StbError("invalid_request", "logic.not requires one argument")
            truth = self._logic_truth(args[0])
            return "x" if truth is None else "0" if truth else "1"
        if op == "logic.is_known":
            return "0" if any(bit in args[0] for bit in "xz") else "1"
        if op == "logic.is_x":
            return "1" if "x" in args[0] else "0"
        if op == "logic.is_z":
            return "1" if "z" in args[0] else "0"
        if op == "bit.not":
            if len(args) != 1:
                raise StbError("invalid_request", "bit.not requires one argument")
            return "".join({"0": "1", "1": "0", "x": "x", "z": "x"}[bit] for bit in args[0])
        if op in {"bit.and", "bit.or", "bit.xor"}:
            if len(args) != 2 or len(args[0]) != len(args[1]):
                raise StbError("invalid_request", f"{op} requires equal-width operands")
            result = []
            for left, right in zip(args[0], args[1]):
                if left in "xz" or right in "xz":
                    result.append("x")
                elif op == "bit.and":
                    result.append("1" if left == right == "1" else "0")
                elif op == "bit.or":
                    result.append("1" if "1" in (left, right) else "0")
                else:
                    result.append("1" if left != right else "0")
            return "".join(result)
        raise StbError("unsupported_capability", f"unsupported expression operator: {op}")

    def _expression_rows(
        self,
        wave: dict[str, Any],
        root: dict[str, Any],
        start: int,
        end: int,
        max_points: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        signals: set[str] = set()
        self._expr_signals(root, signals, [0])
        raw_times = {start, end}
        for signal in signals:
            rows = wave["signals"].get(signal)
            if rows is None:
                raise StbError("signal_not_dumped", f"signal not dumped: {signal}")
            raw_times.update(time for time, _ in rows if start <= time <= end)
        ordered = sorted(raw_times)
        truncated = len(ordered) > max_points
        rows = []
        for time_fs in ordered[:max_points]:
            values = {
                signal: self._value_at(wave["signals"][signal], time_fs)
                for signal in signals
            }
            result = self._eval_expr(root, values)
            rows.append(
                {
                    "time": raw_time_point(time_fs, "1fs"),
                    "value": {
                        "kind": "logic",
                        "width": len(result),
                        "encoding": "bin",
                        "value": result,
                    },
                    "inputs": values,
                }
            )
        return rows, truncated

    def _wave_compute_ir(self, args: dict[str, Any]) -> Any:
        expression = args.get("expression") or {}
        if expression.get("expr_version") != "stb.expr.v1":
            raise StbError("invalid_request", "expr_version must be stb.expr.v1")
        wave_id, wave = self._wave(args.get("wave_id"))
        start = self._raw_time(args["start"])
        end = self._raw_time(args["end"])
        maximum = min(max(1, int(args.get("max_points", 10_000))), 100_000)
        operation = args["operation"]
        if operation == "match_transactions":
            start_root = expression.get("start")
            end_root = expression.get("end")
            if not start_root or not end_root:
                raise StbError("invalid_request", "transaction expression requires start and end")
            start_rows, start_truncated = self._expression_rows(
                wave, start_root, start, end, maximum
            )
            end_rows, end_truncated = self._expression_rows(
                wave, end_root, start, end, maximum
            )
            starts = [row for row in start_rows if row["value"]["value"] == "1"]
            ends = [row for row in end_rows if row["value"]["value"] == "1"]
            pairs = []
            end_index = 0
            for start_row in starts:
                start_tick = int(start_row["time"]["raw_ticks"])
                while end_index < len(ends) and int(ends[end_index]["time"]["raw_ticks"]) < start_tick:
                    end_index += 1
                if end_index >= len(ends):
                    break
                end_row = ends[end_index]
                pairs.append({"start": start_row["time"], "end": end_row["time"]})
                end_index += 1
            return {
                "wave_id": wave_id,
                "operation": operation,
                "transactions": pairs,
                "unmatched_starts": max(0, len(starts) - len(pairs)),
                "truncated": start_truncated or end_truncated,
            }
        root = expression.get("root")
        if not root:
            raise StbError("invalid_request", "expression root is required")
        rows, truncated = self._expression_rows(wave, root, start, end, maximum)
        if operation == "evaluate_window":
            return {
                "wave_id": wave_id,
                "operation": operation,
                "rows": rows,
                "truncated": truncated,
            }
        events = []
        previous = "0"
        edge = args.get("edge", "posedge")
        for row in rows:
            current = row["value"]["value"]
            matched = (
                edge == "posedge" and previous == "0" and current == "1"
            ) or (
                edge == "negedge" and previous == "1" and current == "0"
            ) or (edge == "change" and previous != current)
            if matched:
                events.append(row)
            previous = current
        max_events = max(1, int(args.get("max_events", 1000)))
        return {
            "wave_id": wave_id,
            "operation": operation,
            "edge": edge,
            "events": events[:max_events],
            "truncated": truncated or len(events) > max_events,
        }

    def wave_compute(self, args: dict[str, Any]) -> Any:
        operation = args["operation"]
        if operation == "sample":
            return self.wave_value(args)
        if operation in {"statistics", "find", "xz", "period", "pulse"}:
            wave_id, wave = self._wave(args.get("wave_id"))
            start = self._raw_time(args["start"])
            end = self._raw_time(args["end"])
            items = []
            for signal in args["signals"]:
                rows = wave["signals"].get(signal)
                if rows is None:
                    items.append({"signal": signal, "ok": False, "error_code": "signal_not_dumped"})
                    continue
                selected = [row for row in rows if start <= row[0] <= end]
                data = {"transition_count": max(0, len(selected) - 1), "sample_count": len(selected)}
                if operation == "find":
                    matches = [
                        {"time": raw_time_point(t, "1fs"), "value": v}
                        for t, v in selected
                        if v == args["value"]
                    ]
                    maximum = int(args.get("max_matches", 1000))
                    data = {"matches": matches[:maximum], "truncated": len(matches) > maximum}
                elif operation == "xz":
                    matches = [
                        {"time": raw_time_point(t, "1fs"), "value": v}
                        for t, v in selected
                        if any(bit in v for bit in "xz")
                    ]
                    maximum = int(args.get("max_matches", 1000))
                    data = {"matches": matches[:maximum], "truncated": len(matches) > maximum}
                elif operation == "period":
                    edge = args.get("edge", "posedge")
                    edge_times = []
                    for index in range(1, len(selected)):
                        previous = selected[index - 1][1]
                        current = selected[index][1]
                        if edge == "posedge" and previous == "0" and current == "1":
                            edge_times.append(selected[index][0])
                        elif edge == "negedge" and previous == "1" and current == "0":
                            edge_times.append(selected[index][0])
                    data = {
                        "edge": edge,
                        "edge_count": len(edge_times),
                        "period_ticks": [
                            str(edge_times[index] - edge_times[index - 1])
                            for index in range(1, len(edge_times))
                        ],
                        "scale_unit": "1fs",
                    }
                elif operation == "pulse":
                    data = {
                        "pulses": [
                            {
                                "value": selected[index - 1][1],
                                "start": raw_time_point(selected[index - 1][0], "1fs"),
                                "end": raw_time_point(selected[index][0], "1fs"),
                                "duration_raw_ticks": str(
                                    selected[index][0] - selected[index - 1][0]
                                ),
                            }
                            for index in range(1, len(selected))
                            if selected[index - 1][1] != selected[index][1]
                        ]
                    }
                items.append({"signal": signal, "ok": True, "data": data})
            return {"wave_id": wave_id, "operation": operation, "items": items}
        if operation in {"evaluate_window", "extract_events", "match_transactions"}:
            return self._wave_compute_ir(args)
        if operation in {"compare", "first_divergence"}:
            left_id, left = self._wave(args["left"]["wave_id"])
            right_id, right = self._wave(args["right"]["wave_id"])
            left_signal = args["left"]["signal"]
            right_signal = args["right"]["signal"]
            left_rows = left["signals"].get(left_signal)
            right_rows = right["signals"].get(right_signal)
            if left_rows is None or right_rows is None:
                missing = left_signal if left_rows is None else right_signal
                raise StbError("signal_not_dumped", f"signal not dumped: {missing}")
            if operation == "compare":
                times = [self._raw_time(time_spec) for time_spec in args["times"]]
            else:
                start = self._raw_time(args["start"])
                end = self._raw_time(args["end"])
                times = sorted(
                    {start, end}
                    | {time for time, _ in left_rows if start <= time <= end}
                    | {time for time, _ in right_rows if start <= time <= end}
                )
            rows = []
            for time_fs in times:
                left_value = self._value_at(left_rows, time_fs)
                right_value = self._value_at(right_rows, time_fs)
                row = {
                    "time": f"{time_fs}fs",
                    "left": left_value,
                    "right": right_value,
                    "equal": left_value == right_value,
                }
                rows.append(row)
                if operation == "first_divergence" and not row["equal"]:
                    return {
                        "operation": operation,
                        "left_wave_id": left_id,
                        "right_wave_id": right_id,
                        "divergence": row,
                    }
            return {
                "operation": operation,
                "left_wave_id": left_id,
                "right_wave_id": right_id,
                "rows": rows,
                "divergence": None,
            }
        raise StbError("unsupported_operation", f"fake wave operation: {operation}")

    def source_context(self, args: dict[str, Any]) -> Any:
        ref = args["reference"]
        self.validate_ref(ref)
        assertion = self.assertions.get(ref.get("full_name"))
        if assertion is not None:
            text = "\n".join(
                f"{line}: {value}"
                for line, value in enumerate(
                    assertion["text"].splitlines(),
                    start=assertion["source"]["begin_line"],
                )
            )
            preprocessor = {
                "available": False,
                "reason": "fake_fixture",
                "macros": [],
                "includes": [],
            }
            expansion_context = self._expansion_context(
                assertion["source"]["file"],
                assertion["source"],
                preprocessor,
            )
            return {
                "reference": ref,
                "source": assertion["source"],
                "start_line": assertion["source"]["begin_line"],
                "end_line": assertion["source"]["end_line"],
                "text": text,
                "truncated": False,
                "fingerprint": "fake:assertion-source:v1",
                "current_fingerprint": "fake:assertion-source:v1",
                "loaded_fingerprint": "fake:assertion-source:v1",
                "change_status": "unchanged",
                "source_alignment": "aligned",
                "source_variant": "current",
                "expansion_context_id": expansion_context["expansion_context_id"],
                "expansion_context": expansion_context,
                "preprocessor_evidence": preprocessor,
            }
        if ref.get("full_name") not in self.objects:
            raise StbError("source_unavailable", "fake object has no source")
        preprocessor = {
            "available": False,
            "reason": "fake_fixture",
            "macros": [],
            "includes": [],
        }
        expansion_context = self._expansion_context(
            "fake/top.sv",
            {"include_chain": [], "begin_line": 1},
            preprocessor,
        )
        requested_expansion_id = args.get("expansion_context_id")
        if (
            requested_expansion_id
            and requested_expansion_id != expansion_context["expansion_context_id"]
        ):
            raise StbError(
                "invalid_request",
                "expansion_context_id does not match the resolved source context",
                {
                    "requested": requested_expansion_id,
                    "actual": expansion_context["expansion_context_id"],
                },
            )
        return {
            "reference": ref,
            "source": {"file": "fake/top.sv", "begin_line": 1, "end_line": 1, "include_chain": []},
            "start_line": 1,
            "end_line": 1,
            "text": "1: module top;",
            "truncated": False,
            "fingerprint": "fake:source:v1",
            "current_fingerprint": "fake:source:v1",
            "loaded_fingerprint": "fake:source:v1",
            "change_status": "unchanged",
            "source_alignment": "aligned",
            "source_variant": "current",
            "expansion_context_id": expansion_context["expansion_context_id"],
            "expansion_context": expansion_context,
            "preprocessor_evidence": preprocessor,
        }

    def assertion_structure(self, args: dict[str, Any]) -> Any:
        ref = args["reference"]
        self.validate_ref(ref)
        if ref.get("model") != "language" or ref.get("npi_type") != "npiAssert":
            raise StbError(
                "unsupported_capability",
                "assertion_structure requires an npiAssert language reference",
            )
        assertion = self.assertions.get(ref.get("full_name"))
        if assertion is None:
            raise StbError("object_not_found", "fake assertion not found")
        structure = parse_assertion_source(assertion["text"])

        def resolve(token: str) -> dict[str, Any] | None:
            obj = self.objects.get(token)
            return self._summary(obj) if obj is not None else None

        structure = resolve_structure_dependencies(structure, resolve)
        source = self.source_context({"reference": ref})
        return {
            "schema_version": "stb.assertion-structure.v1",
            "capability": {
                "status": "available",
                "probe": "fake_fixture",
            },
            "anchor": self._assertion_summary(assertion),
            "source_evidence": {
                **source,
                "declaration_text": structure["assertion"]["raw"],
            },
            "npi_cross_reference": {
                "assertion_type": "npiAssert",
                "property_type": "npiPropertySpec",
                "property_declaration": None,
            },
            "structure": structure,
            "truncated": False,
        }

    def _expansion_context(
        self,
        physical_file: str,
        source: dict[str, Any],
        preprocessor: dict[str, Any],
    ) -> dict[str, Any]:
        macro_material = {
            "macros": preprocessor.get("macros") or [],
            "includes": preprocessor.get("includes") or [],
        }
        macro_fingerprint = hashlib.sha256(
            json.dumps(macro_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        identity = {
            "context_id": self.context_id,
            "worker_generation": self.generation,
            "physical_file": physical_file,
            "include_site": {
                "file": physical_file,
                "line": source.get("begin_line"),
            },
            "include_chain": source.get("include_chain") or [],
            "macro_environment_fingerprint": macro_fingerprint,
        }
        expansion_id = "exp-" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        return {
            "expansion_context_id": expansion_id,
            **identity,
            "macro_environment": {
                "fingerprint": macro_fingerprint,
                "status": "unavailable"
                if not preprocessor.get("available")
                else "bounded_relevant",
                "complete": False,
                "reason": "complete macro environment is not available in fake backend",
            },
        }

    def mapping(self, args: dict[str, Any]) -> Any:
        if args["action"] == "validate":
            return {
                "valid": True,
                "rule_count": len((args.get("profile") or {}).get("rules") or []),
            }
        cache_key = json.dumps(
            {
                "generation": self.generation,
                "wave_generations": {
                    name: wave["generation"] for name, wave in sorted(self.waves.items())
                },
                "args": args,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if cache_key in self._mapping_cache:
            result = dict(self._mapping_cache[cache_key])
            result["cache"] = "hit"
            if not result.pop("ok", True):
                raise StbError(**result["error"])
            return result
        wave_id, wave = self._wave(args.get("wave_id"))
        design = args.get("design_full_name")
        waveform = args.get("waveform_full_name") or design
        matched = design in self.objects and waveform in wave["signals"]
        pipeline = [
            {
                "rule": "full_name",
                "candidate": waveform,
                "status": "matched" if matched else "not_found",
            }
        ]
        if design not in self.objects or waveform not in wave["signals"]:
            error = StbError("mapping_not_found", "fake exact mapping not found")
            self._mapping_cache[cache_key] = {"ok": False, "error": error.as_dict()}
            raise error
        width = len(wave["signals"][waveform][0][1])
        result = {
            "action": args["action"],
            "context_mode": args.get("context_mode", "same"),
            "wave_id": wave_id,
            "wave_generation": wave["generation"],
            "design_full_name": design,
            "waveform_full_name": waveform,
            "rule": "full_name",
            "pipeline": pipeline,
            "actual_name_evidence": {
                "status": "unavailable",
                "reason": "fake_fixture",
            },
            "bit_mapping": {
                "kind": "identity",
                "design_range": [0, width - 1],
                "waveform_range": [0, width - 1],
                "direction": "ascending",
            },
            "cache": "miss",
        }
        self._mapping_cache[cache_key] = {"ok": True, **dict(result)}
        return result

    def trace_active_driver(self, args: dict[str, Any]) -> Any:
        return {
            "wave_id": args.get("wave_id"),
            "time": args.get("time"),
            "temporal_resolution": "structural_only",
            "layers": {
                "compile_time": {"status": "fake_fixture"},
                "elaboration_time": {"status": "fake_fixture"},
                "runtime": {"status": "unavailable"},
                "resolution": {"status": "fake_fixture"},
                "simulation_override": {"status": "not_recorded"},
            },
            "items": [
                {
                    "signal": signal,
                    "ok": signal in self.objects,
                    "branches": [],
                    "error_code": None
                    if signal in self.objects
                    else "object_not_found",
                }
                for signal in args.get("signals", [])
            ],
        }

    def trace_value_origin(self, args: dict[str, Any]) -> Any:
        return {
            "wave_id": args.get("wave_id"),
            "time": args.get("time"),
            "items": [
                {
                    "signal": signal,
                    "ok": signal in self.objects,
                    "hops": [],
                    "stop_reason": "fake_fixture",
                    "error_code": None
                    if signal in self.objects
                    else "object_not_found",
                }
                for signal in args.get("signals", [])
            ],
        }
