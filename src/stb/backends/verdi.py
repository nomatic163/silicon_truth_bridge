from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from stb.assertions import (
    parse_assertion_source,
    resolve_structure_dependencies,
    strip_numbered_source,
)
from stb.backends.base import Backend
from stb.cursors import CursorRegistry
from stb.errors import StbError
from stb.query import QUERY_OPERATORS, evaluate_where
from stb.timeutil import raw_time_point, to_raw_tick
from stb.verdi_compat import (
    VERIFIED_VERDI_RELEASES,
    detect_verdi_release,
    is_verified_release,
    missing_npi_symbols,
    unexpected_npi_module_origins,
)


class VerdiBackend(Backend):
    name = "python_npi"
    verified_releases = VERIFIED_VERDI_RELEASES

    def __init__(
        self,
        context_id: str,
        verdi_home: str,
        verdi_release: str | None = None,
        generation: int = 1,
        design_spec: dict[str, Any] | None = None,
        wave_specs: list[dict[str, Any]] | None = None,
        allowed_roots: list[str] | None = None,
        max_object_handles: int = 100_000,
        allow_unverified_verdi: bool = False,
    ) -> None:
        self.context_id = context_id
        self.generation = generation
        self._context_open_time_ns = time.time_ns()
        self.verdi_home = Path(verdi_home)
        self.verdi_version = detect_verdi_release(
            self.verdi_home,
            override=verdi_release,
        )
        self.verified_verdi = is_verified_release(self.verdi_version)
        self.verdi_compatibility = (
            "verified" if self.verified_verdi else "unverified"
        )
        if not self.verified_verdi and not allow_unverified_verdi:
            raise StbError(
                "unsupported_api_version",
                f"Verdi release {self.verdi_version} is not verified for STB V1",
                {
                    "verified_releases": sorted(self.verified_releases),
                    "detected": self.verdi_version,
                    "opt_in": "STB_ALLOW_UNVERIFIED_VERDI=1",
                },
            )
        # pynpi validates VERDI_HOME independently of the STB configuration.
        os.environ["VERDI_HOME"] = str(self.verdi_home)
        npi_python = self.verdi_home / "share/NPI/python"
        if not npi_python.is_dir():
            raise StbError(
                "unsupported_api_version",
                f"Verdi Python NPI directory is missing for {self.verdi_version}",
                {
                    "detected": self.verdi_version,
                    "npi_python": str(npi_python),
                },
                recoverable=False,
            )
        if str(npi_python) not in sys.path:
            sys.path.insert(0, str(npi_python))
        try:
            from pynpi import lang, netlist, npisys, text, waveform
        except (ImportError, OSError) as exc:
            raise StbError(
                "unsupported_api_version",
                f"Verdi Python NPI could not be imported for {self.verdi_version}",
                {
                    "detected": self.verdi_version,
                    "worker_python": sys.executable,
                    "reason": str(exc),
                },
                recoverable=False,
            ) from exc

        modules = {
            "lang": lang,
            "netlist": netlist,
            "npisys": npisys,
            "text": text,
            "waveform": waveform,
        }
        unexpected_origins = unexpected_npi_module_origins(npi_python, modules)
        if unexpected_origins:
            raise StbError(
                "unsupported_api_version",
                "Verdi Python NPI was loaded from a different installation",
                {
                    "detected": self.verdi_version,
                    "unexpected_module_origins": unexpected_origins,
                },
                recoverable=False,
            )
        missing_symbols = missing_npi_symbols(modules)
        if missing_symbols:
            raise StbError(
                "unsupported_api_version",
                f"Verdi Python NPI is incompatible with STB for {self.verdi_version}",
                {
                    "detected": self.verdi_version,
                    "missing_symbols": missing_symbols,
                },
                recoverable=False,
            )

        self.lang = lang
        self.netlist = netlist
        self.npisys = npisys
        self.text = text
        self.waveform = waveform
        self.design_spec = design_spec
        self.allowed_roots = [
            Path(item).expanduser().resolve() for item in (allowed_roots or [])
        ]
        self.max_object_handles = max_object_handles
        self._language_handles: OrderedDict[str, Any] = OrderedDict()
        self._language_keys: dict[tuple[Any, ...], str] = {}
        self._next_object_id = 1
        self._source_fingerprints: dict[str, str] = {}
        self._source_snapshots: dict[str, dict[str, Any]] = {}
        self._query_cache: OrderedDict[str, Any] = OrderedDict()
        self._mapping_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._handle_value_cache: OrderedDict[tuple[int, str, tuple[Any, ...]], Any] = (
            OrderedDict()
        )
        self._source_cache: OrderedDict[int, dict[str, Any] | None] = OrderedDict()
        self._semantic_cache: OrderedDict[int, tuple[str | None, str | None]] = (
            OrderedDict()
        )
        self._assertion_capability_cache: dict[str, Any] | None = None
        self._design_summary_cache: OrderedDict[tuple[str, str], dict[str, Any]] = (
            OrderedDict()
        )
        self._inst_children_cache: OrderedDict[str, tuple[Any, ...]] = OrderedDict()
        self._inst_local_cache: OrderedDict[str, tuple[Any, ...]] = OrderedDict()
        self._wave_changes_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cursors = CursorRegistry()
        self._request_metrics = {
            "npi_calls": 0,
            "npi_ms": 0.0,
            "cache_hits": 0,
            "cache_misses": 0,
        }
        self._objects_by_name: dict[str, list[Any]] = {}
        self._all_design_objects: list[Any] = []
        self._top_design_objects: list[Any] = []
        self.waves: dict[str, dict[str, Any]] = {}
        npi_argv = list(design_spec.get("argv") or []) if design_spec else ["stb-worker"]
        if design_spec and design_spec.get("top") and "-top" not in npi_argv:
            npi_argv.extend(["-top", str(design_spec["top"])])
        if design_spec and design_spec.get("cwd"):
            design_cwd = Path(design_spec["cwd"]).expanduser().resolve()
            self._assert_allowed_path(design_cwd)
            if not design_cwd.is_dir():
                raise StbError("path_not_allowed", f"design cwd is not a directory: {design_cwd}")
            os.chdir(design_cwd)
        if not self.npisys.init(npi_argv):
            raise StbError("worker_internal_error", "npisys.init failed", recoverable=False)
        self._initialized = True
        if design_spec:
            # SP1 mutates the argv list during init; load_design must receive
            # that same processed list rather than a fresh copy.
            if not self.npisys.load_design(npi_argv):
                self.close()
                raise StbError("design_load_failed", "npisys.load_design failed")
            self._index_design()
        discovered_resources = self._design_resource_paths(design_spec)
        self._source_search_resources = list(discovered_resources)
        design_argv = [str(value) for value in (design_spec or {}).get("argv") or []]
        self._precompiled_db = "-dbdir" in design_argv
        if design_spec and not self._precompiled_db:
            self._source_search_resources.extend(self._text_resource_paths())
        self._source_search_resources = list(
            dict.fromkeys(self._source_search_resources)
        )
        source_suffixes = {
            ".v",
            ".sv",
            ".vh",
            ".svh",
            ".vhd",
            ".vhdl",
            ".c",
            ".cc",
            ".cpp",
            ".h",
            ".hpp",
        }
        self._design_resources = [
            path
            for path in discovered_resources
            if path.is_dir() or path.suffix.lower() not in source_suffixes
        ]
        self._design_resource_fingerprints = {
            str(path): self._fingerprint(path) for path in self._design_resources
        }
        self.design_fingerprint = self._combined_fingerprint(
            self._design_resource_fingerprints
        )
        for spec in wave_specs or []:
            self._attach_wave(spec["wave_id"], spec["path"])

    def _npi_call(self, label: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        del label  # Reserved for sampled span tracing; receipt keeps only aggregates.
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            self._request_metrics["npi_calls"] += 1
            self._request_metrics["npi_ms"] += (time.perf_counter() - started) * 1000

    def _record_npi_elapsed(self, started: float, calls: int = 1) -> None:
        self._request_metrics["npi_calls"] += max(0, int(calls))
        self._request_metrics["npi_ms"] += (time.perf_counter() - started) * 1000

    def _lru_put(self, cache: OrderedDict[Any, Any], key: Any, value: Any, limit: int) -> Any:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
        return value

    def _clear_handle_caches(self) -> None:
        self._handle_value_cache.clear()
        self._source_cache.clear()
        self._semantic_cache.clear()
        self._assertion_capability_cache = None
        self._design_summary_cache.clear()
        self._inst_children_cache.clear()
        self._inst_local_cache.clear()

    def _drop_handle_caches(self, handle: Any) -> None:
        handle_id = id(handle)
        for cache in (
            self._handle_value_cache,
            self._source_cache,
            self._semantic_cache,
            self._design_summary_cache,
        ):
            for key in list(cache.keys()):
                if key == handle_id or (
                    isinstance(key, tuple) and key and key[0] == handle_id
                ):
                    cache.pop(key, None)

    def _assert_allowed_path(self, path: Path) -> None:
        if not self.allowed_roots:
            return
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise StbError(
                "source_outside_allowed_roots",
                f"path is outside allowed roots: {path}",
            )

    def _fingerprint(self, path: Path) -> str:
        try:
            stat = path.stat()
        except OSError as exc:
            raise StbError("resource_changed", f"resource is unavailable: {path}") from exc
        kind = "dir" if path.is_dir() else "file"
        return f"meta:{kind}:{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"

    def _combined_fingerprint(self, values: dict[str, str]) -> str | None:
        if not values:
            return None
        digest = hashlib.sha256()
        for path, value in sorted(values.items()):
            digest.update(path.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(value.encode("ascii"))
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def _design_resource_paths(
        self, design_spec: dict[str, Any] | None
    ) -> list[Path]:
        resources = []
        for value in (design_spec or {}).get("argv") or []:
            text = str(value)
            if text.startswith("+incdir+"):
                for directory in text[len("+incdir+") :].split("+"):
                    candidate = Path(directory).expanduser()
                    if candidate.exists():
                        resolved = candidate.resolve()
                        self._assert_allowed_path(resolved)
                        resources.append(resolved)
                continue
            candidate = Path(text).expanduser()
            if candidate.exists():
                resolved = candidate.resolve()
                self._assert_allowed_path(resolved)
                resources.append(resolved)
        return resources

    def _text_resource_paths(self) -> list[Path]:
        resources = []
        try:
            files = self._npi_call("text.get_file_list", self.text.get_file_list) or []
        except Exception:
            return resources
        for handle in files:
            try:
                path = (
                    Path(self._handle_value(handle, "file_full_name"))
                    .expanduser()
                    .resolve()
                )
            except Exception:
                continue
            if path.exists():
                self._assert_allowed_path(path)
                resources.append(path)
        return resources

    def check_resources(
        self, operation: str | None = None, args: dict[str, Any] | None = None
    ) -> None:
        args = args or {}
        wave_only = operation in {"wave_value", "wave_changes", "wave_compute"}
        if operation in {"object_resolve", "object_query"}:
            wave_only = args.get("model") == "waveform"
        elif operation in {"object_get", "object_traverse"}:
            refs = args.get("references") or args.get("roots") or []
            wave_only = bool(refs) and all(
                ref.get("model") == "waveform" for ref in refs
            )
        if wave_only or operation in {"catalog"}:
            return
        for path_text, expected in self._design_resource_fingerprints.items():
            path = Path(path_text)
            if self._fingerprint(path) != expected:
                raise StbError(
                    "resource_changed",
                    f"design resource changed: {path}",
                    {"resource": str(path), "kind": "design"},
                )

    def wave_generation(self, wave_id: str | None) -> int | None:
        try:
            _, wave = self._wave(wave_id)
        except StbError:
            return None
        return int(wave["generation"])

    def _attach_wave(self, wave_id: str, path: str) -> dict[str, Any]:
        if wave_id in self.waves:
            raise StbError("invalid_request", f"wave already attached: {wave_id}")
        resolved_path = Path(path).expanduser().resolve()
        self._assert_allowed_path(resolved_path)
        fingerprint = self._fingerprint(resolved_path)
        handle = self._npi_call("waveform.open", self.waveform.open, str(resolved_path))
        if handle is None:
            raise StbError("wave_open_failed", f"failed to open waveform: {resolved_path}")
        self.waves[wave_id] = {
            "path": str(resolved_path),
            "handle": handle,
            "generation": 1,
            "fingerprint": fingerprint,
            "signal_cache": OrderedDict(),
        }
        self._query_cache.clear()
        self._mapping_cache.clear()
        self._wave_changes_cache.clear()
        self._cursors.clear()
        return self._wave_info(wave_id)

    def _wave(
        self, wave_id: str | None, check_resource: bool = True
    ) -> tuple[str, dict[str, Any]]:
        if wave_id is None:
            if len(self.waves) != 1:
                raise StbError("invalid_request", "wave_id is required")
            wave_id = next(iter(self.waves))
        wave = self.waves.get(wave_id)
        if wave is None:
            raise StbError("wave_not_found", f"wave not found: {wave_id}")
        if check_resource:
            path = Path(wave["path"]).resolve()
            if self._fingerprint(path) != wave["fingerprint"]:
                raise StbError(
                    "resource_changed",
                    f"waveform resource changed: {path}",
                    {"resource": str(path), "kind": "waveform", "wave_id": wave_id},
                )
        return wave_id, wave

    def _wave_info(self, wave_id: str) -> dict[str, Any]:
        wave = self.waves[wave_id]
        handle = wave["handle"]
        return {
            "wave_id": wave_id,
            "path": wave["path"],
            "fingerprint": wave["fingerprint"],
            "wave_generation": wave["generation"],
            "name": self._handle_value(handle, "name"),
            "min_time": str(self._handle_value(handle, "min_time")),
            "max_time": str(self._handle_value(handle, "max_time")),
            "scale_unit": self._handle_value(handle, "scale_unit"),
            "version": self._handle_value(handle, "version"),
            "sim_date": self._handle_value(handle, "sim_date"),
            "is_completed": bool(self._handle_value(handle, "is_completed")),
            "has_glitch": bool(self._handle_value(handle, "has_glitch")),
            "has_seq_num": bool(self._handle_value(handle, "has_seq_num")),
            "has_reason_code": bool(self._handle_value(handle, "has_reason_code")),
            "has_force_tag": bool(self._handle_value(handle, "has_force_tag")),
        }

    def _wave_signal(self, wave: dict[str, Any], name: str) -> Any:
        cache = wave["signal_cache"]
        if name in cache:
            self._request_metrics["cache_hits"] += 1
            cache.move_to_end(name)
            return cache[name]
        self._request_metrics["cache_misses"] += 1
        cache[name] = self._npi_call(
            "waveform.file.sig_by_name",
            wave["handle"].sig_by_name,
            name,
        )
        cache.move_to_end(name)
        while len(cache) > 4096:
            cache.popitem(last=False)
        return cache[name]

    def _query_cache_get(self, key: str) -> Any:
        value = self._query_cache.get(key)
        if value is not None:
            self._request_metrics["cache_hits"] += 1
            self._query_cache.move_to_end(key)
        else:
            self._request_metrics["cache_misses"] += 1
        return value

    def _query_cache_put(self, key: str, value: Any) -> Any:
        self._query_cache[key] = value
        self._query_cache.move_to_end(key)
        while len(self._query_cache) > 256:
            self._query_cache.popitem(last=False)
        return value

    def _wave_changes_cache_get(self, key: str) -> dict[str, Any] | None:
        cached = self._wave_changes_cache.get(key)
        if cached is None:
            self._request_metrics["cache_misses"] += 1
            return None
        self._request_metrics["cache_hits"] += 1
        self._wave_changes_cache.move_to_end(key)
        result = dict(cached["result"])
        cursor_state = cached.get("cursor_state")
        result["next_cursor"] = (
            self._cursors.issue(cursor_state) if cursor_state else None
        )
        return result

    def _wave_changes_cache_put(
        self,
        key: str,
        result: dict[str, Any],
        cursor_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        stored = dict(result)
        stored["next_cursor"] = None
        self._wave_changes_cache[key] = {
            "result": stored,
            "cursor_state": cursor_state,
        }
        self._wave_changes_cache.move_to_end(key)
        while len(self._wave_changes_cache) > 64:
            self._wave_changes_cache.popitem(last=False)
        return result

    def reset_request_metrics(self) -> None:
        self._request_metrics = {
            "npi_calls": 0,
            "npi_ms": 0.0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

    def request_metrics(self) -> dict[str, int]:
        return dict(self._request_metrics)

    def _index_design(self) -> None:
        self._top_design_objects = list(
            self._npi_call("netlist.get_top_inst_list", self.netlist.get_top_inst_list)
        )
        for item in self._top_design_objects:
            self._cache_design_object(item)

    def _design_full_name(self, obj: Any) -> str:
        return str(self._handle_value(obj, "full_name"))

    def _design_name(self, obj: Any) -> str:
        return str(self._handle_value(obj, "name"))

    def _cache_design_object(self, obj: Any) -> None:
        self._remember_design_object(
            obj,
            self._canonical_type(obj),
            self._design_full_name(obj),
        )

    def _remember_design_object(self, obj: Any, npi_type: str, full_name: str) -> None:
        name = full_name
        cached = self._objects_by_name.setdefault(name, [])
        key = npi_type
        if not any(self._canonical_type(item) == key for item in cached):
            cached.append(obj)
            self._all_design_objects.append(obj)

    def _instance_local_objects(self, inst: Any) -> list[Any]:
        cache_key = self._design_full_name(inst)
        cached = self._inst_local_cache.get(cache_key)
        if cached is not None:
            self._inst_local_cache.move_to_end(cache_key)
            return list(cached)
        objects = []
        for method_name in ("port_list", "instport_list", "net_list"):
            method = getattr(inst, method_name, None)
            if method is not None:
                objects.extend(
                    self._npi_call(f"netlist.inst.{method_name}", method) or []
                )
        return list(self._lru_put(self._inst_local_cache, cache_key, tuple(objects), 8192))

    def _inst_children(self, inst: Any) -> list[Any]:
        cache_key = self._design_full_name(inst)
        cached = self._inst_children_cache.get(cache_key)
        if cached is not None:
            self._inst_children_cache.move_to_end(cache_key)
            return list(cached)
        children = tuple(self._npi_call("netlist.inst.inst_list", inst.inst_list) or [])
        return list(self._lru_put(self._inst_children_cache, cache_key, children, 8192))

    def _iter_design_objects(
        self,
        scope: str | None,
        max_scan: int,
        include_local_objects: bool = True,
        cache_objects: bool = True,
    ):
        if scope:
            root = self._resolve_design(scope, "INST")
            roots = [root]
        else:
            roots = list(self._top_design_objects)
        stack = list(reversed(roots))
        scanned = 0
        while stack and scanned < max_scan:
            inst = stack.pop()
            if cache_objects:
                self._cache_design_object(inst)
            scanned += 1
            yield inst
            if scanned >= max_scan:
                break
            if include_local_objects:
                for item in self._instance_local_objects(inst):
                    if cache_objects:
                        self._cache_design_object(item)
                    scanned += 1
                    yield item
                    if scanned >= max_scan:
                        break
                if scanned >= max_scan:
                    break
            children = self._inst_children(inst)
            if cache_objects:
                for child in children:
                    self._cache_design_object(child)
            stack.extend(reversed(children))

    def _canonical_type(self, obj: Any, assumed_kind: str | None = None) -> str:
        if assumed_kind is not None:
            return assumed_kind
        raw = self._handle_value(obj, "type", False)
        return {
            "npiNlInst": "INST",
            "npiNlPort": "PORT",
            "npiNlInstPort": "INSTPORT",
            "npiNlDeclNet": "DECL_NET",
            "npiNlConcatNet": "CONCAT_NET",
            "npiNlSliceNet": "SLICE_NET",
            "npiNlPseudoPort": "PSEUDO_PORT",
            "npiNlPseudoInstPort": "PSEUDO_INSTPORT",
            "npiNlPseudoNet": "PSEUDO_NET",
        }.get(raw, raw)

    def _semantic_class(
        self, obj: Any, assumed_kind: str | None = None
    ) -> tuple[str | None, str | None]:
        cache_key = id(obj)
        cached = self._semantic_cache.get(cache_key)
        if cached is not None:
            self._semantic_cache.move_to_end(cache_key)
            return cached
        kind = self._canonical_type(obj, assumed_kind)
        result: tuple[str | None, str | None] = (None, None)
        if kind == "INST":
            inst_type = self._handle_value(obj, "inst_type", False)
            if inst_type == "npiNlHierInst":
                result = ("module_instance", "inst_type=npiNlHierInst")
                return self._lru_put(self._semantic_cache, cache_key, result, 200_000)
            cell_type = self._handle_value(obj, "cell_type", False)
            if cell_type == "npiNlFlipFlopCell":
                result = ("register", "cell_type=npiNlFlipFlopCell")
                return self._lru_put(self._semantic_cache, cache_key, result, 200_000)
            if "Latch" in str(cell_type):
                result = ("latch", f"cell_type={cell_type}")
                return self._lru_put(self._semantic_cache, cache_key, result, 200_000)
            if self._handle_value(obj, "is_memory_cell"):
                result = ("memory", "is_memory_cell=true")
                return self._lru_put(self._semantic_cache, cache_key, result, 200_000)
        if kind in {"DECL_NET", "CONCAT_NET", "SLICE_NET"}:
            result = ("combinational_net", f"npi_type={kind}")
        return self._lru_put(self._semantic_cache, cache_key, result, 200_000)

    def _matches_semantic(
        self,
        obj: Any,
        wanted: set[str],
        assumed_kind: str | None = None,
    ) -> bool:
        if not wanted:
            return True
        if assumed_kind == "INST" and wanted <= {
            "module_instance",
            "register",
            "latch",
            "memory",
        }:
            if "module_instance" in wanted:
                inst_type = self._handle_value(obj, "inst_type", False)
                if inst_type == "npiNlHierInst":
                    return True
            if wanted & {"register", "latch"}:
                cell_type = str(self._handle_value(obj, "cell_type", False))
                if "register" in wanted and cell_type == "npiNlFlipFlopCell":
                    return True
                if "latch" in wanted and "Latch" in cell_type:
                    return True
            if "memory" in wanted and self._handle_value(obj, "is_memory_cell"):
                return True
            return False
        semantic, _ = self._semantic_class(obj, assumed_kind)
        return semantic in wanted

    def _source(self, obj: Any, cache: bool = True) -> dict[str, Any] | None:
        cache_key = id(obj)
        if cache and cache_key in self._source_cache:
            self._source_cache.move_to_end(cache_key)
            return self._source_cache[cache_key]

        def store(value: dict[str, Any] | None) -> dict[str, Any] | None:
            if not cache:
                return value
            return self._lru_put(self._source_cache, cache_key, value, 200_000)

        if not hasattr(obj, "file"):
            return store(None)
        try:
            file_name = self._handle_value(obj, "file", cache=cache)
            begin_method = getattr(obj, "begin_line_no", None) or getattr(
                obj, "begin_line", None
            )
            end_method = getattr(obj, "end_line_no", None) or getattr(
                obj, "end_line", None
            )
            if begin_method is None or end_method is None:
                return store(None)
            begin = int(
                self._npi_call(
                    f"{type(obj).__name__}.{getattr(begin_method, '__name__', 'begin_line')}",
                    begin_method,
                )
            )
            end = int(
                self._npi_call(
                    f"{type(obj).__name__}.{getattr(end_method, '__name__', 'end_line')}",
                    end_method,
                )
            )
        except Exception:
            return store(None)
        if not file_name or begin <= 0:
            return store(None)
        return store(
            {
                "file": file_name,
                "begin_line": begin,
                "end_line": max(begin, end),
                "include_chain": [],
            }
        )

    def _resolve_source_path(self, reported_file: str) -> Path:
        source_path = Path(reported_file).expanduser()
        if source_path.is_absolute():
            return source_path.resolve()
        candidates = [Path.cwd() / source_path]
        for resource in self._source_search_resources:
            base = resource if resource.is_dir() else resource.parent
            candidates.append(base / source_path)
            if resource.is_dir():
                candidates.append(resource.parent / source_path)
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.is_file():
                self._assert_allowed_path(resolved)
                return resolved
        return candidates[0].resolve()

    def _handle_value(
        self,
        handle: Any,
        method_name: str,
        *args: Any,
        default: Any = None,
        cache: bool = True,
    ) -> Any:
        key = (id(handle), method_name, tuple(args))
        if cache and key in self._handle_value_cache:
            self._handle_value_cache.move_to_end(key)
            return self._handle_value_cache[key]
        method = getattr(handle, method_name, None)
        if method is None:
            return default
        value = self._npi_call(
            f"{type(handle).__name__}.{method_name}",
            method,
            *args,
        )
        if not cache:
            return value
        return self._lru_put(self._handle_value_cache, key, value, 200_000)

    def _safe_handle_value(
        self,
        handle: Any,
        method_name: str,
        default: Any = None,
        cache: bool = True,
    ) -> Any:
        method = getattr(handle, method_name, None)
        if method is None:
            return default
        try:
            return self._handle_value(
                handle,
                method_name,
                default=default,
                cache=cache,
            )
        except Exception:
            return default

    def _language_type(self, handle: Any) -> str:
        try:
            return str(self._handle_value(handle, "type", False, cache=False))
        except Exception:
            return type(handle).__name__

    def _language_info(self, handle: Any) -> str:
        try:
            value = self._npi_call("lang.get_hdl_info", self.lang.get_hdl_info, handle, True)
        except Exception:
            try:
                value = self._npi_call("lang.get_hdl_info", self.lang.get_hdl_info, handle)
            except Exception:
                return ""
        return str(value)

    def _language_full_name(self, handle: Any) -> str | None:
        value = self._safe_handle_value(handle, "full_name", cache=False)
        if value:
            return str(value)
        info = self._language_info(handle)
        match = re.match(r"^[^,]+,\s*([^,]+),\s*\{", info)
        if match is None:
            return None
        candidate = match.group(1).strip()
        return None if candidate in {"", "(null)"} else candidate

    def _language_source(self, handle: Any) -> dict[str, Any] | None:
        source = self._source(handle, cache=False)
        if source is not None:
            return source
        info = self._language_info(handle)
        match = re.search(r"\{(.+?)\s*:\s*(\d+)(?:\s*-\s*(\d+))?\}", info)
        if not match:
            return None
        return {
            "file": match.group(1).strip(),
            "begin_line": int(match.group(2)),
            "end_line": int(match.group(3) or match.group(2)),
            "include_chain": [],
        }

    def _language_key(self, handle: Any) -> tuple[Any, ...]:
        source = self._language_source(handle) or {}
        return (
            self._language_type(handle),
            self._language_full_name(handle),
            self._safe_handle_value(handle, "name", cache=False),
            source.get("file"),
            source.get("begin_line"),
            source.get("end_line"),
            self._language_info(handle),
        )

    def _retain_language(self, handle: Any) -> str:
        if handle is None:
            raise StbError("object_not_found", "language handle is null")
        key = self._language_key(handle)
        existing = self._language_keys.get(key)
        if existing is not None:
            return existing
        if len(self._language_handles) >= self.max_object_handles:
            raise StbError(
                "object_handle_limit_reached",
                "language object handle limit reached",
                {"limit": self.max_object_handles},
            )
        object_id = f"lang-{self.generation}-{self._next_object_id}"
        self._next_object_id += 1
        self._language_handles[object_id] = handle
        self._language_keys[key] = object_id
        return object_id

    def _resolve_language_ref(self, ref: dict[str, Any]) -> Any:
        if "context_id" in ref or "worker_generation" in ref:
            self.validate_ref(ref)
        object_id = ref.get("object_id")
        if object_id:
            handle = self._language_handles.get(object_id)
            if handle is None:
                raise StbError("stale_object_id", f"unknown language object: {object_id}")
            return handle
        name = ref.get("full_name") or ref.get("name")
        if not name:
            raise StbError("invalid_request", "language reference requires object_id or name")
        handle = self._npi_call("lang.handle_by_name", self.lang.handle_by_name, name, None)
        if handle is None:
            raise StbError("object_not_found", f"language object not found: {name}")
        return handle

    def _expression_text(self, handle: Any, max_chars: int = 2000) -> str | None:
        try:
            text = self._npi_call("lang.expr_decompile", self.lang.expr_decompile, handle)
        except Exception:
            return None
        if text is None:
            return None
        return str(text)[:max_chars]

    def _language_summary(self, handle: Any) -> dict[str, Any]:
        object_id = self._retain_language(handle)
        full_name = self._language_full_name(handle)
        name = self._safe_handle_value(handle, "name", cache=False)
        npi_type = self._language_type(handle)
        semantic = {
            "npiAssignment": "assignment",
            "npiContAssign": "continuous_assignment",
            "npiEventControl": "event_control",
            "npiIf": "if_statement",
            "npiCase": "case_statement",
            "npiOperation": "expression",
            "npiAssert": "concurrent_assertion",
            "npiPropertyDecl": "property_declaration",
        }.get(npi_type, "language_object")
        return {
            "ref": {
                "model": "language",
                "context_id": self.context_id,
                "worker_generation": self.generation,
                "npi_type": npi_type,
                "full_name": full_name or None,
                "object_id": object_id,
            },
            "name": name or None,
            "semantic_class": semantic,
            "classification_rule": f"npi_type={npi_type}",
            "source": self._language_source(handle),
            "description": self._expression_text(handle),
        }

    def _language_scope_handle(self, design_obj: Any) -> Any:
        candidates = [
            self._safe_handle_value(design_obj, "full_name"),
            self._safe_handle_value(design_obj, "def_name"),
            self._safe_handle_value(design_obj, "name"),
        ]
        for name in dict.fromkeys(str(item) for item in candidates if item):
            try:
                handle = self._npi_call(
                    "lang.handle_by_name",
                    self.lang.handle_by_name,
                    name,
                    None,
                )
            except Exception:
                handle = None
            if handle is not None and hasattr(handle, "assertion_handles"):
                return handle
            if handle is not None:
                try:
                    self._npi_call("lang.release_handle", self.lang.release_handle, handle)
                except Exception:
                    pass
        return None

    def _iter_assertion_handles(self, scope: str | None, max_scan: int):
        seen: set[tuple[Any, ...]] = set()
        for design_obj in self._iter_design_objects(
            scope,
            max_scan,
            include_local_objects=False,
            cache_objects=False,
        ):
            if self._canonical_type(design_obj) != "INST":
                continue
            language_scope = self._language_scope_handle(design_obj)
            if language_scope is None:
                continue
            try:
                handles = self._npi_call(
                    "lang.scope.assertion_handles",
                    language_scope.assertion_handles,
                ) or []
            except Exception:
                handles = []
            self._retain_language(language_scope)
            for handle in handles:
                key = self._language_key(handle)
                if key in seen:
                    try:
                        self._npi_call(
                            "lang.release_handle",
                            self.lang.release_handle,
                            handle,
                        )
                    except Exception:
                        pass
                    continue
                seen.add(key)
                self._retain_language(handle)
                yield handle

    def _assertion_capability(self) -> dict[str, Any]:
        if self._assertion_capability_cache is not None:
            return dict(self._assertion_capability_cache)
        if not self.design_spec:
            result = {
                "status": "unavailable",
                "reason": "design_not_loaded",
                "object_discovery": "unavailable",
                "source_anchor": "unavailable",
            }
            self._assertion_capability_cache = result
            return dict(result)

        relation_found = False
        assertion_count = 0
        anchored_count = 0
        for design_obj in self._iter_design_objects(
            None,
            64,
            include_local_objects=False,
            cache_objects=False,
        ):
            if self._canonical_type(design_obj) != "INST":
                continue
            language_scope = self._language_scope_handle(design_obj)
            if language_scope is None:
                continue
            relation_found = hasattr(language_scope, "assertion_handles")
            try:
                handles = self._npi_call(
                    "lang.scope.assertion_handles",
                    language_scope.assertion_handles,
                ) or []
            except Exception:
                handles = []
            finally:
                try:
                    self._npi_call(
                        "lang.release_handle",
                        self.lang.release_handle,
                        language_scope,
                    )
                except Exception:
                    pass
            for handle in handles:
                assertion_count += 1
                if self._language_source(handle) is not None:
                    anchored_count += 1
                try:
                    self._npi_call("lang.release_handle", self.lang.release_handle, handle)
                except Exception:
                    pass
            if relation_found:
                break

        if not relation_found:
            result = {
                "status": "unavailable",
                "reason": "assertion_relation_unavailable",
                "object_discovery": "unavailable",
                "source_anchor": "unavailable",
            }
        elif assertion_count and not anchored_count:
            result = {
                "status": "unavailable",
                "reason": "assertion_source_anchor_unavailable",
                "object_discovery": "available",
                "source_anchor": "unavailable",
                "probed_assertions": assertion_count,
            }
        else:
            result = {
                "status": "available",
                "object_discovery": "available",
                "source_anchor": (
                    "verified" if anchored_count else "not_applicable_no_assertions"
                ),
                "probed_assertions": assertion_count,
                "anchored_assertions": anchored_count,
            }
        self._assertion_capability_cache = result
        return dict(result)

    def _design_summary(self, obj: Any) -> dict[str, Any]:
        npi_type = self._canonical_type(obj)
        full_name = self._design_full_name(obj)
        cache_key = (npi_type, full_name)
        cached = self._design_summary_cache.get(cache_key)
        if cached is not None:
            self._design_summary_cache.move_to_end(cache_key)
            return cached
        self._remember_design_object(obj, npi_type, full_name)
        semantic, rule = self._semantic_class(obj)
        return self._lru_put(self._design_summary_cache, cache_key, {
            "ref": {
                "model": "netlist",
                "context_id": self.context_id,
                "worker_generation": self.generation,
                "npi_type": npi_type,
                "full_name": full_name,
            },
            "name": self._design_name(obj),
            "semantic_class": semantic,
            "classification_rule": rule,
            "source": self._source(obj),
        }, 200_000)

    def _resolve_design(self, name: str, npi_type: str | None = None) -> Any:
        candidates = self._objects_by_name.get(name, [])
        if not candidates and npi_type == "INST" and hasattr(self.netlist, "get_inst"):
            try:
                inst = self._npi_call("netlist.get_inst", self.netlist.get_inst, name)
            except Exception:
                inst = None
            if inst is not None:
                self._cache_design_object(inst)
                candidates = self._objects_by_name.get(name, [])
        if not candidates:
            matching_tops = [
                item
                for item in self._top_design_objects
                if name == self._design_full_name(item)
                or name.startswith(self._design_full_name(item) + ".")
            ]
            for top in matching_tops:
                current = top
                if self._design_full_name(current) == name:
                    self._cache_design_object(current)
                    break
                while current is not None:
                    found = False
                    child_match = None
                    for child in self._inst_children(current):
                        self._cache_design_object(child)
                        child_name = self._design_full_name(child)
                        if name == child_name or name.startswith(child_name + "."):
                            child_match = child
                            break
                    if child_match is None:
                        for item in self._instance_local_objects(current):
                            self._cache_design_object(item)
                            if self._design_full_name(item) == name:
                                found = True
                        break
                    if self._design_full_name(child_match) == name:
                        found = True
                        break
                    current = child_match
                if found:
                    break
            candidates = self._objects_by_name.get(name, [])
        if npi_type:
            candidates = [
                item for item in candidates if self._canonical_type(item) == npi_type
            ]
        if not candidates:
            raise StbError("object_not_found", f"design object not found: {name}")
        if len(candidates) > 1:
            raise StbError(
                "ambiguous_name",
                f"multiple design objects share full_name: {name}",
                {"types": [self._canonical_type(item) for item in candidates]},
            )
        return candidates[0]

    def _resolve_design_signal(
        self, name: str, npi_type: str | None = "DECL_NET"
    ) -> tuple[Any, str]:
        try:
            return self._resolve_design(name, npi_type), "exact"
        except StbError as exc:
            if exc.code != "object_not_found" or re.search(r"\[\d+\]$", name):
                raise
        bit_zero = self._resolve_design(f"{name}[0]", npi_type)
        try:
            self._resolve_design(f"{name}[1]", npi_type)
        except StbError as exc:
            if exc.code == "object_not_found":
                return bit_zero, "singleton_bit_expansion"
            raise
        raise StbError(
            "ambiguous_name",
            f"aggregate signal has multiple bit-blasted design objects: {name}",
            {"guidance": "use an explicit bit or bit_mode=expand"},
        )

    def close(self) -> None:
        for handle in list(self._language_handles.values()):
            try:
                self._npi_call("lang.release_handle", self.lang.release_handle, handle)
            except Exception:
                pass
            self._drop_handle_caches(handle)
        self._language_handles.clear()
        self._language_keys.clear()
        self._query_cache.clear()
        self._mapping_cache.clear()
        self._wave_changes_cache.clear()
        self._clear_handle_caches()
        self._cursors.clear()
        for wave in list(self.waves.values()):
            try:
                self._npi_call("waveform.close", self.waveform.close, wave["handle"])
            except Exception:
                pass
        self.waves.clear()
        if getattr(self, "_initialized", False):
            try:
                self._npi_call("npisys.end", self.npisys.end)
            finally:
                self._initialized = False

    def release_objects(self, object_ids: list[str]) -> Any:
        released = []
        missing = []
        for object_id in object_ids:
            handle = self._language_handles.pop(object_id, None)
            if handle is None:
                missing.append(object_id)
                continue
            self._language_keys.pop(self._language_key(handle), None)
            try:
                self._npi_call("lang.release_handle", self.lang.release_handle, handle)
            except Exception:
                pass
            self._drop_handle_caches(handle)
            released.append(object_id)
        if released:
            self._query_cache.clear()
        return {"released": released, "missing": missing}

    def wave_manage(self, args: dict[str, Any]) -> Any:
        action = args["action"]
        wave_id = args.get("wave_id")
        if action == "attach":
            return self._attach_wave(wave_id, args["path"])
        if action == "list":
            return {"waves": [self._wave_info(item) for item in sorted(self.waves)]}
        if action == "status":
            resolved, _ = self._wave(wave_id)
            return self._wave_info(resolved)
        if action == "detach":
            resolved, wave = self._wave(wave_id, check_resource=False)
            self._npi_call("waveform.close", self.waveform.close, wave["handle"])
            del self.waves[resolved]
            self._query_cache.clear()
            self._mapping_cache.clear()
            self._wave_changes_cache.clear()
            self._clear_handle_caches()
            self._cursors.clear()
            return {"wave_id": resolved, "state": "detached"}
        if action == "reload":
            resolved, wave = self._wave(wave_id, check_resource=False)
            path = args.get("path") or wave["path"]
            generation = wave["generation"] + 1
            self._npi_call("waveform.close", self.waveform.close, wave["handle"])
            del self.waves[resolved]
            self._clear_handle_caches()
            result = self._attach_wave(resolved, path)
            self.waves[resolved]["generation"] = generation
            result["wave_generation"] = generation
            self._mapping_cache.clear()
            return result
        raise StbError("unsupported_operation", f"unsupported wave action: {action}")

    def catalog(self, kind: str, filters: dict[str, Any]) -> Any:
        values = {
            "models": ["netlist", "language", "waveform"],
            "object_types": [
                name for name in dir(self.netlist.ObjectType) if name.isupper()
            ]
            + ["npiAssert", "npiPropertyDecl"],
            "backend_capabilities": {
                "backend": self.name,
                "real_npi": True,
                "verdi_version": self.verdi_version,
                "verdi_compatibility": self.verdi_compatibility,
                "verified_verdi": self.verified_verdi,
                "verified_releases": sorted(self.verified_releases),
                "design_loaded": bool(self.design_spec),
                "wave_count": len(self.waves),
                "assertion_structure": self._assertion_capability(),
            },
            "properties": [
                "name",
                "full_name",
                "type",
                "def_name",
                "direction",
                "left_range",
                "right_range",
                "range_size",
                "is_real",
                "is_string",
                "is_packed",
                "has_member",
            ],
            "relations": [
                "netlist.inst.children",
                "netlist.inst.ports",
                "netlist.inst.instports",
                "netlist.inst.nets",
                "netlist.net.drivers",
                "netlist.net.loads",
                "netlist.net.fanin_registers",
                "netlist.net.fanout_registers",
                "language.assignment.lhs",
                "language.assignment.rhs",
                "language.statement.condition",
                "language.event.statement",
                "language.object.scope",
                "language.object.parent",
                "waveform.scope.child_scopes",
                "waveform.scope.signals",
                "waveform.signal.members",
            ],
            "semantic_classes": [
                "module_instance",
                "register",
                "latch",
                "memory",
                "combinational_net",
                "assignment",
                "continuous_assignment",
                "event_control",
                "if_statement",
                "case_statement",
                "expression",
                "waveform_scope",
                "waveform_signal",
                "concurrent_assertion",
                "property_declaration",
            ],
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
                "max_object_handles": self.max_object_handles,
                "default_query_limit": 100,
                "default_trace_nodes": 1000,
                "default_wave_changes": 1000,
                "hard_expression_nodes": 1000,
                "hard_evaluation_points": 100_000,
            },
        }
        if kind not in values:
            raise StbError("unsupported_capability", f"catalog not implemented: {kind}")
        return values[kind]

    def object_resolve(self, args: dict[str, Any]) -> Any:
        model = args.get("model", "netlist")
        if model == "language":
            return self._language_summary(self._resolve_language_ref(args))
        if model != "waveform":
            obj = self._resolve_design(args["name"], args.get("npi_type"))
            return self._design_summary(obj)
        wave_id, wave = self._wave(args.get("wave_id"))
        handle = wave["handle"]
        name = args["name"]
        obj = self._wave_signal(wave, name)
        kind = "SIGNAL"
        if obj is None:
            obj = self._npi_call("waveform.file.scope_by_name", handle.scope_by_name, name)
            kind = "SCOPE"
        if obj is None:
            raise StbError("object_not_found", f"waveform object not found: {name}")
        return self._wave_summary(wave_id, obj, kind)

    def object_get(self, args: dict[str, Any]) -> Any:
        results = []
        for ref in args["references"]:
            try:
                self.validate_ref(ref)
                model = ref.get("model", "netlist")
                if model == "waveform":
                    summary = self.object_resolve(
                        {
                            "model": "waveform",
                            "wave_id": args.get("wave_id"),
                            "name": ref["full_name"],
                        }
                    )
                    _, wave = self._wave(args.get("wave_id"))
                    handle = wave["handle"]
                    obj = self._wave_signal(
                        wave, ref["full_name"]
                    ) or self._npi_call(
                        "waveform.file.scope_by_name",
                        handle.scope_by_name,
                        ref["full_name"],
                    )
                elif model == "language":
                    obj = self._resolve_language_ref(ref)
                    summary = self._language_summary(obj)
                else:
                    obj = self._resolve_design(ref["full_name"], ref.get("npi_type"))
                    summary = self._design_summary(obj)
                props = {}
                for prop in args.get("properties", []):
                    if model == "language" and prop == "expression_text":
                        props[prop] = self._expression_text(
                            obj, int(args.get("max_chars", 2000))
                        )
                        continue
                    method = getattr(obj, prop, None)
                    if method is None or not callable(method):
                        props[prop] = {"error_code": "property_not_supported"}
                        continue
                    try:
                        props[prop] = (
                            self._npi_call(
                                f"{type(obj).__name__}.{prop}",
                                method,
                                False,
                            )
                            if prop in {"type", "direction"}
                            else self._npi_call(
                                f"{type(obj).__name__}.{prop}",
                                method,
                            )
                        )
                    except Exception:
                        props[prop] = {"error_code": "property_not_supported"}
                results.append({"ok": True, "summary": summary, "properties": props})
            except StbError as exc:
                results.append({"ok": False, "error_code": exc.code})
        return results

    def object_query(self, args: dict[str, Any]) -> Any:
        model = args.get("model", "netlist")
        wave_generation = None
        if model == "waveform":
            _, selected_wave = self._wave(args.get("wave_id"))
            wave_generation = selected_wave["generation"]
        cache_key = json.dumps(
            {
                "worker_generation": self.generation,
                "wave_generation": wave_generation,
                "model": model,
                "scope": args.get("scope"),
                "npi_types": sorted(args.get("npi_types") or []),
                "semantic_classes": sorted(args.get("semantic_classes") or []),
                "where": args.get("where"),
                "limit": args.get("limit"),
                "cursor": args.get("cursor"),
                "allow_global": args.get("allow_global"),
                "max_scan": args.get("max_scan"),
                "wave_id": args.get("wave_id"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        cached = self._query_cache_get(cache_key)
        if cached is not None:
            return cached
        query_key = {
            "operation": "object_query",
            "context_id": self.context_id,
            "worker_generation": self.generation,
            "model": model,
            "scope": args.get("scope"),
            "npi_types": sorted(args.get("npi_types") or []),
            "semantic_classes": sorted(args.get("semantic_classes") or []),
            "where": args.get("where"),
            "wave_id": args.get("wave_id"),
        }
        cursor_state = self._cursors.get(args.get("cursor"))
        offset = 0
        if cursor_state is not None:
            if cursor_state.get("key") != query_key:
                raise StbError("cursor_mismatch", "cursor does not match object query")
            offset = int(cursor_state.get("offset", 0))
        if model == "language":
            scope = args.get("scope")
            wanted_types = set(args.get("npi_types") or [])
            wanted_semantic = set(args.get("semantic_classes") or [])
            limit = int(args.get("limit", 100))
            matches = []
            seen: set[tuple[Any, ...]] = set()
            matched_count = 0
            max_scan = min(max(1, int(args.get("max_scan", 100_000))), 1_000_000)
            assertion_query = bool(
                wanted_types & {"npiAssert"}
                or wanted_semantic & {"concurrent_assertion"}
            )
            if assertion_query:
                for handle in self._iter_assertion_handles(scope, max_scan):
                    key = self._language_key(handle)
                    if key in seen:
                        continue
                    seen.add(key)
                    if wanted_types and self._language_type(handle) not in wanted_types:
                        try:
                            self._npi_call(
                                "lang.release_handle",
                                self.lang.release_handle,
                                handle,
                            )
                        except Exception:
                            pass
                        continue
                    summary = self._language_summary(handle)
                    if wanted_semantic and summary["semantic_class"] not in wanted_semantic:
                        continue
                    if matched_count < offset:
                        matched_count += 1
                        continue
                    matches.append(summary)
                    if len(matches) >= limit + 1:
                        break
                truncated = len(matches) > limit
                return self._query_cache_put(
                    cache_key,
                    {
                        "objects": matches[:limit],
                        "truncated": truncated,
                        "next_cursor": self._cursors.issue(
                            {"key": query_key, "offset": offset + limit}
                        )
                        if truncated
                        else None,
                    },
                )
            for design_obj in self._iter_design_objects(
                scope, max_scan, include_local_objects=True
            ):
                name = self._design_full_name(design_obj)
                try:
                    handle = self._npi_call(
                        "lang.handle_by_name",
                        self.lang.handle_by_name,
                        name,
                        None,
                    )
                except Exception:
                    handle = None
                if handle is None:
                    continue
                key = self._language_key(handle)
                if key in seen:
                    continue
                seen.add(key)
                if wanted_types and self._language_type(handle) not in wanted_types:
                    try:
                        self._npi_call(
                            "lang.release_handle", self.lang.release_handle, handle
                        )
                    except Exception:
                        pass
                    self._drop_handle_caches(handle)
                    continue
                if not evaluate_where(
                    args.get("where"),
                    lambda prop, current=handle: self._language_query_property(
                        current, prop
                    ),
                ):
                    try:
                        self._npi_call(
                            "lang.release_handle", self.lang.release_handle, handle
                        )
                    except Exception:
                        pass
                    self._drop_handle_caches(handle)
                    continue
                if matched_count < offset:
                    matched_count += 1
                    try:
                        self._npi_call(
                            "lang.release_handle", self.lang.release_handle, handle
                        )
                    except Exception:
                        pass
                    self._drop_handle_caches(handle)
                    continue
                matches.append(self._language_summary(handle))
                if len(matches) >= limit + 1:
                    break
            truncated = len(matches) > limit
            return self._query_cache_put(cache_key, {
                "objects": matches[:limit],
                "truncated": truncated,
                "next_cursor": self._cursors.issue(
                    {"key": query_key, "offset": offset + limit}
                )
                if truncated
                else None,
            })
        if model != "waveform":
            scope = args.get("scope")
            if not scope and not args.get("allow_global", False):
                raise StbError(
                    "invalid_request",
                    "global design query requires allow_global=true",
                )
            wanted_types = set(args.get("npi_types") or [])
            wanted_semantic = set(args.get("semantic_classes") or [])
            limit = int(args.get("limit", 100))
            max_scan = min(max(1, int(args.get("max_scan", 100_000))), 1_000_000)
            matches = []
            matched_count = 0
            scanned = 0
            where = args.get("where")
            instance_only = (
                wanted_types == {"INST"}
                or (
                    not wanted_types
                    and bool(wanted_semantic)
                    and wanted_semantic
                    <= {"module_instance", "register", "latch", "memory"}
                )
            )
            for obj in self._iter_design_objects(
                scope,
                max_scan,
                include_local_objects=not instance_only,
                cache_objects=False,
            ):
                scanned += 1
                assumed_kind = "INST" if instance_only else None
                if (
                    wanted_types
                    and not instance_only
                    and self._canonical_type(obj) not in wanted_types
                ):
                    continue
                if wanted_semantic:
                    if not self._matches_semantic(
                        obj,
                        wanted_semantic,
                        assumed_kind,
                    ):
                        continue
                if not evaluate_where(
                    where,
                    lambda prop, current=obj: self._design_query_property(
                        current, prop
                    ),
                ):
                    continue
                if matched_count < offset:
                    matched_count += 1
                    continue
                matches.append(self._design_summary(obj))
                if len(matches) >= limit + 1:
                    break
            truncated = len(matches) > limit
            return self._query_cache_put(cache_key, {
                "objects": matches[:limit],
                "truncated": truncated,
                "scanned": scanned,
                "termination_reason": "scan_limit"
                if scanned >= max_scan and not truncated
                else None,
                "next_cursor": self._cursors.issue(
                    {"key": query_key, "offset": offset + limit}
                )
                if truncated
                else None,
            })
        wave_id, wave = self._wave(args.get("wave_id"))
        handle = wave["handle"]
        scope_name = args.get("scope")
        roots = (
            [
                self._npi_call(
                    "waveform.file.scope_by_name",
                    handle.scope_by_name,
                    scope_name,
                )
            ]
            if scope_name
            else self._npi_call("waveform.file.top_scope_list", handle.top_scope_list)
        )
        roots = [root for root in roots if root is not None]
        limit = int(args.get("limit", 100))
        wanted = set(args.get("npi_types") or ["SCOPE", "SIGNAL"])
        matches = []
        matched_count = 0
        stack = list(reversed(roots))
        while stack and len(matches) < limit + 1:
            scope = stack.pop()
            if "SCOPE" in wanted:
                scope_matches = evaluate_where(
                    args.get("where"),
                    lambda prop, current=scope: self._wave_query_property(
                        current, "SCOPE", prop
                    ),
                )
                if scope_matches and matched_count < offset:
                    matched_count += 1
                elif scope_matches:
                    matches.append(self._wave_summary(wave_id, scope, "SCOPE"))
            for signal in self._npi_call("waveform.scope.sig_list", scope.sig_list) or []:
                if "SIGNAL" in wanted:
                    signal_matches = evaluate_where(
                        args.get("where"),
                        lambda prop, current=signal: self._wave_query_property(
                            current, "SIGNAL", prop
                        ),
                    )
                    if not signal_matches:
                        continue
                    if matched_count < offset:
                        matched_count += 1
                    else:
                        matches.append(self._wave_summary(wave_id, signal, "SIGNAL"))
                    if len(matches) >= limit + 1:
                        break
            stack.extend(
                reversed(
                    self._npi_call(
                        "waveform.scope.child_scope_list", scope.child_scope_list
                    )
                    or []
                )
            )
        truncated = len(matches) > limit
        return self._query_cache_put(cache_key, {
            "objects": matches[:limit],
            "truncated": truncated,
            "next_cursor": self._cursors.issue(
                {"key": query_key, "offset": offset + limit}
            )
            if truncated
            else None,
        })

    def _design_query_property(self, obj: Any, prop: str) -> Any:
        if prop == "name":
            return self._design_name(obj)
        if prop == "full_name":
            return self._design_full_name(obj)
        if prop == "npi_type":
            return self._canonical_type(obj)
        if prop == "semantic_class":
            semantic, _ = self._semantic_class(obj)
            return semantic
        if prop not in {
            "def_name",
            "direction",
            "left_range",
            "right_range",
            "range_size",
            "is_real",
            "is_string",
            "is_packed",
            "has_member",
        }:
            raise StbError("invalid_request", f"property is not queryable: {prop}")
        return self._safe_handle_value(obj, prop)

    def _language_query_property(self, obj: Any, prop: str) -> Any:
        if prop == "name":
            return self._safe_handle_value(obj, "name", cache=False)
        if prop == "full_name":
            return self._safe_handle_value(obj, "full_name", cache=False)
        if prop == "npi_type":
            return self._language_type(obj)
        if prop in {"source_file", "begin_line"}:
            source = self._language_source(obj)
            return (
                source.get("file" if prop == "source_file" else "begin_line")
                if source
                else None
            )
        raise StbError("invalid_request", f"property is not queryable: {prop}")

    def _wave_query_property(self, obj: Any, kind: str, prop: str) -> Any:
        values = {
            "name": self._safe_handle_value(obj, "name"),
            "full_name": self._safe_handle_value(obj, "full_name"),
            "npi_type": kind,
            "range_size": self._safe_handle_value(obj, "range_size"),
        }
        if prop not in values:
            raise StbError("invalid_request", f"property is not queryable: {prop}")
        return values[prop]

    def object_traverse(self, args: dict[str, Any]) -> Any:
        relation = args["relation"]
        depth = int(args.get("depth", 1))
        maximum = int(args.get("max_nodes", 1000))
        request_key = {
            "operation": "object_traverse",
            "context_id": self.context_id,
            "worker_generation": self.generation,
            "relation": relation,
            "depth": depth,
            "wave_id": args.get("wave_id"),
            "roots": args["roots"],
            "filters": args.get("filters"),
        }
        cursor_state = self._cursors.get(args.get("cursor"))
        if cursor_state is not None:
            if cursor_state.get("key") != request_key:
                raise StbError("cursor_mismatch", "cursor does not match traversal")
            queue = list(cursor_state["queue"])
            pending = list(cursor_state["pending"])
            seen = set(cursor_state["seen"])
            scanned = int(cursor_state.get("scanned", 0))
        else:
            for ref in args["roots"]:
                self.validate_ref(ref)
            queue = [{"ref": ref, "level": 0} for ref in args["roots"]]
            pending: list[dict[str, Any]] = []
            seen = {self._ref_key(ref) for ref in args["roots"]}
            scanned = 0

        results = []
        termination_reason = None
        while len(results) < maximum and (pending or queue):
            if self.soft_timed_out():
                termination_reason = "soft_timeout"
                break
            while pending and len(results) < maximum:
                if self.soft_timed_out():
                    termination_reason = "soft_timeout"
                    break
                summary = pending.pop(0)
                if self._summary_matches(summary, args.get("filters")):
                    results.append(summary)
            if termination_reason:
                break
            if len(results) >= maximum or not queue:
                break
            current = queue.pop(0)
            if int(current["level"]) >= depth:
                continue
            children = self._relation_children(
                current["ref"], relation, args.get("wave_id")
            )
            scanned += len(children)
            for summary in children:
                ref = summary["ref"]
                key = self._ref_key(ref)
                if key in seen:
                    continue
                seen.add(key)
                pending.append(summary)
                queue.append({"ref": ref, "level": int(current["level"]) + 1})

        truncated = bool(pending or queue)
        next_cursor = None
        if truncated:
            next_cursor = self._cursors.issue(
                {
                    "key": request_key,
                    "queue": queue,
                    "pending": pending,
                    "seen": sorted(seen),
                    "scanned": scanned,
                }
            )
        return {
            "objects": results,
            "truncated": truncated,
            "scanned": scanned,
            "returned": len(results),
            "termination_reason": termination_reason
            or ("node_limit" if truncated else None),
            "next_cursor": next_cursor,
        }

    def _ref_key(self, ref: dict[str, Any]) -> str:
        return "|".join(
            str(ref.get(key) or "")
            for key in ("model", "npi_type", "full_name", "object_id")
        )

    def _summary_matches(
        self, summary: dict[str, Any], filters: dict[str, Any] | None
    ) -> bool:
        if not filters:
            return True
        where = filters.get("where") if "where" in filters else filters
        values = {
            "name": summary.get("name"),
            "full_name": summary["ref"].get("full_name"),
            "npi_type": summary["ref"].get("npi_type"),
            "semantic_class": summary.get("semantic_class"),
        }
        return evaluate_where(
            where,
            lambda prop: values[prop]
            if prop in values
            else (_ for _ in ()).throw(
                StbError("invalid_request", f"property is not queryable: {prop}")
            ),
        )

    def _relation_children(
        self, ref: dict[str, Any], relation: str, selected_wave_id: str | None
    ) -> list[dict[str, Any]]:
        model = ref.get("model", "netlist")
        if model == "language":
            obj = self._resolve_language_ref(ref)
            relations = {
                "language.assignment.lhs": "lhs_handle",
                "language.assignment.rhs": "rhs_handle",
                "language.statement.condition": "condition_handle",
                "language.event.statement": "stmt_handle",
                "language.object.scope": "scope_handle",
                "language.object.parent": "parent_handle",
            }
            method_name = relations.get(relation)
            method = getattr(obj, method_name, None) if method_name else None
            if method is None:
                raise StbError("relation_not_supported", relation)
            child = self._npi_call(f"{type(obj).__name__}.{method_name}", method)
            if child is None:
                return []
            values = child if isinstance(child, (list, tuple)) else [child]
            return [self._language_summary(item) for item in values]
        if model != "waveform":
            obj = self._resolve_design(ref["full_name"], ref.get("npi_type"))
            relations = {
                "netlist.inst.children": "inst_list",
                "netlist.inst.ports": "port_list",
                "netlist.inst.instports": "instport_list",
                "netlist.inst.nets": "net_list",
                "netlist.net.drivers": "driver_list",
                "netlist.net.loads": "load_list",
                "netlist.net.fanin_registers": "fan_in_reg_list",
                "netlist.net.fanout_registers": "fan_out_reg_list",
            }
            method_name = relations.get(relation)
            if method_name is None or not hasattr(obj, method_name):
                raise StbError("relation_not_supported", relation)
            return [
                self._design_summary(child)
                for child in self._npi_call(
                    f"{type(obj).__name__}.{method_name}",
                    getattr(obj, method_name),
                )
                or []
            ]
        wave_id, wave = self._wave(selected_wave_id)
        handle = wave["handle"]
        if ref["npi_type"] == "SCOPE":
            obj = self._npi_call(
                "waveform.file.scope_by_name",
                handle.scope_by_name,
                ref["full_name"],
            )
            if relation == "waveform.scope.child_scopes":
                values = [
                    self._wave_summary(wave_id, child, "SCOPE")
                    for child in self._npi_call(
                        "waveform.scope.child_scope_list",
                        obj.child_scope_list,
                    )
                    or []
                ]
            elif relation == "waveform.scope.signals":
                values = [
                    self._wave_summary(wave_id, child, "SIGNAL")
                    for child in self._npi_call(
                        "waveform.scope.sig_list",
                        obj.sig_list,
                    )
                    or []
                ]
            else:
                raise StbError("relation_not_supported", relation)
            return values
        if relation != "waveform.signal.members":
            raise StbError("relation_not_supported", relation)
        obj = self._wave_signal(wave, ref["full_name"])
        return [
            self._wave_summary(wave_id, child, "SIGNAL")
            for child in self._npi_call("waveform.signal.member_list", obj.member_list)
            or []
        ]

    def connectivity_direct(self, args: dict[str, Any]) -> Any:
        method = "driver_list" if args["kind"] == "driver" else "load_list"
        items = []
        for signal in args["signals"]:
            try:
                obj, resolution_rule = self._resolve_design_signal(
                    signal, args.get("npi_type", "DECL_NET")
                )
                related = []
                for item in self._npi_call(
                    f"{type(obj).__name__}.{method}",
                    getattr(obj, method),
                ) or []:
                    entry = self._design_summary(item)
                    if hasattr(item, "scope_inst"):
                        try:
                            owner = self._npi_call(
                                f"{type(item).__name__}.scope_inst",
                                item.scope_inst,
                            )
                        except Exception:
                            owner = None
                        if owner is not None:
                            entry["owner"] = self._design_summary(owner)
                    related.append(entry)
                items.append(
                    {
                        "signal": signal,
                        "ok": True,
                        "resolved": self._design_summary(obj),
                        "resolution_rule": resolution_rule,
                        "objects": related,
                    }
                )
            except StbError as exc:
                items.append({"signal": signal, "ok": False, "error_code": exc.code})
        return {"kind": args["kind"], "items": items}

    def _language_trace_records(self, signal: str, kind: str) -> list[dict[str, Any]]:
        trace_func = (
            self.lang.trace_driver2 if kind == "driver" else self.lang.trace_load2
        )
        try:
            statements = self._npi_call(f"lang.trace_{kind}", trace_func, signal)
        except Exception as exc:
            raise StbError(
                "language_trace_failed",
                f"Language NPI {kind} trace failed for {signal}: {exc}",
            ) from exc
        records = []
        for statement in statements or []:
            src = self._npi_call(
                f"{type(statement).__name__}.get_src_hdl",
                statement.get_src_hdl,
            )
            use = self._npi_call(
                f"{type(statement).__name__}.get_use_hdl",
                statement.get_use_hdl,
            )
            scope = self._npi_call(
                f"{type(statement).__name__}.get_scope_hdl",
                statement.get_scope_hdl,
            )
            signal_handles = list(
                self._npi_call(
                    f"{type(statement).__name__}.get_sig_hdl_list",
                    statement.get_sig_hdl_list,
                )
                or []
            )
            record = {
                "source": self._language_summary(src) if src is not None else None,
                "statement": self._language_summary(use) if use is not None else None,
                "scope": self._language_summary(scope) if scope is not None else None,
                "signals": [
                    self._language_summary(handle) for handle in signal_handles
                ],
                "is_pass_through": bool(
                    self._npi_call(
                        f"{type(statement).__name__}.get_is_pass_thr",
                        statement.get_is_pass_thr,
                    )
                ),
                "signal_use_count": int(
                    self._npi_call(
                        f"{type(statement).__name__}.get_num_sig_use",
                        statement.get_num_sig_use,
                    )
                ),
            }
            if use is not None:
                for key, method_name in (
                    ("lhs", "lhs_handle"),
                    ("rhs", "rhs_handle"),
                    ("condition", "condition_handle"),
                    ("event_statement", "stmt_handle"),
                ):
                    method = getattr(use, method_name, None)
                    if method is None:
                        continue
                    try:
                        related = self._npi_call(
                            f"{type(use).__name__}.{method_name}",
                            method,
                        )
                    except Exception:
                        related = None
                    if related is not None:
                        record[key] = self._language_summary(related)
                ancestors = []
                child = use
                for _ in range(16):
                    parent = None
                    for method_name in ("upper_stmt_handle", "parent_handle"):
                        method = getattr(child, method_name, None)
                        if method is None:
                            continue
                        try:
                            parent = self._npi_call(
                                f"{type(child).__name__}.{method_name}",
                                method,
                            )
                        except Exception:
                            parent = None
                        if parent is not None:
                            break
                    if parent is None:
                        break
                    parent_type = self._language_type(parent)
                    role = "body"
                    if parent_type in {"npiIf", "npiIfElse"}:
                        for candidate_role, method_name in (
                            ("true", "stmt_handle"),
                            ("false", "else_stmt_handle"),
                        ):
                            method = getattr(parent, method_name, None)
                            if method is None:
                                continue
                            try:
                                branch = self._npi_call(
                                    f"{type(parent).__name__}.{method_name}",
                                    method,
                                )
                            except Exception:
                                branch = None
                            if (
                                branch is not None
                                and self._language_key(branch)
                                == self._language_key(child)
                            ):
                                role = candidate_role
                                break
                    ancestors.append(
                        {
                            "object": self._language_summary(parent),
                            "branch_role": role,
                        }
                    )
                    child = parent
                record["ancestors"] = ancestors
            records.append(record)
        return records

    def trace(self, args: dict[str, Any]) -> Any:
        kind = args["kind"]
        if kind not in {"driver", "load", "path", "fanin", "fanout"}:
            raise StbError("unsupported_operation", f"trace kind not implemented: {kind}")
        relation = {
            "driver": "driver_list",
            "load": "load_list",
            "path": "load_list",
            "fanin": "driver_list",
            "fanout": "load_list",
        }[kind]
        max_depth = int(args.get("max_depth", 20))
        max_nodes = int(args.get("max_nodes", 1000))
        target_names = {
            item.get("full_name") if isinstance(item, dict) else item
            for item in args.get("targets") or []
        }
        stop_names = {
            item.get("full_name") if isinstance(item, dict) else item
            for item in args.get("stop_at") or []
        }
        for ref in args["roots"] + args.get("targets", []) + args.get("stop_at", []):
            if isinstance(ref, dict):
                self.validate_ref(ref)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        terminals: list[dict[str, Any]] = []
        node_ids: dict[tuple[str, str], str] = {}

        def add_node(obj: Any) -> str:
            key = (self._canonical_type(obj), self._design_full_name(obj))
            if key not in node_ids:
                node_id = f"g{len(node_ids) + 1}"
                node_ids[key] = node_id
                nodes.append(
                    {"node_id": node_id, "origin": "npi", **self._design_summary(obj)}
                )
            return node_ids[key]

        queue: list[tuple[Any, int]] = []
        roots = []
        for root in args["roots"]:
            obj, _ = self._resolve_design_signal(
                root["full_name"] if isinstance(root, dict) else root,
                root.get("npi_type", "DECL_NET") if isinstance(root, dict) else "DECL_NET",
            )
            root_id = add_node(obj)
            roots.append(root_id)
            queue.append((obj, 0))
            if kind in {"driver", "load"}:
                for record in self._language_trace_records(
                    self._design_full_name(obj), kind
                ):
                    statement = record.get("statement")
                    if statement is None or len(nodes) >= max_nodes:
                        continue
                    lang_node_id = f"g{len(node_ids) + 1}"
                    lang_key = (
                        "language",
                        statement["ref"].get("object_id") or lang_node_id,
                    )
                    if lang_key in node_ids:
                        lang_node_id = node_ids[lang_key]
                    else:
                        node_ids[lang_key] = lang_node_id
                        nodes.append(
                            {
                                "node_id": lang_node_id,
                                "origin": "npi_language",
                                **statement,
                            }
                        )
                    edges.append(
                        {
                            "from": lang_node_id
                            if kind == "driver"
                            else root_id,
                            "to": root_id
                            if kind == "driver"
                            else lang_node_id,
                            "relation": "language_driver"
                            if kind == "driver"
                            else "language_load",
                        }
                    )
        visited: set[tuple[str, str]] = set()
        truncated = False
        while queue:
            obj, depth = queue.pop(0)
            key = (self._canonical_type(obj), self._design_full_name(obj))
            if key in visited:
                continue
            visited.add(key)
            current = add_node(obj)
            full_name = self._design_full_name(obj)
            if full_name in target_names:
                terminals.append({"node_id": current, "reason": "target_reached"})
                if kind == "path":
                    continue
            if full_name in stop_names:
                terminals.append({"node_id": current, "reason": "stop_object"})
                continue
            if depth >= max_depth:
                terminals.append({"node_id": current, "reason": "depth_limit"})
                continue
            if not hasattr(obj, relation):
                terminals.append({"node_id": current, "reason": "unsupported_object"})
                continue
            related = list(
                self._npi_call(
                    f"{type(obj).__name__}.{relation}",
                    getattr(obj, relation),
                )
                or []
            )
            if not related:
                terminals.append(
                    {
                        "node_id": current,
                        "reason": "primary_input"
                        if kind in {"driver", "fanin"}
                        else "no_load",
                    }
                )
            for child in related:
                if len(nodes) >= max_nodes:
                    truncated = True
                    terminals.append({"node_id": current, "reason": "node_limit"})
                    queue.clear()
                    break
                child_id = add_node(child)
                edges.append(
                    {
                        "from": child_id if kind in {"driver", "fanin"} else current,
                        "to": current if kind in {"driver", "fanin"} else child_id,
                        "relation": "drives" if kind in {"driver", "fanin"} else "loads",
                    }
                )
                queue.append((child, depth + 1))
        return {
            "roots": roots,
            "nodes": nodes,
            "edges": edges,
            "terminals": terminals,
            "limits": {
                "max_depth": max_depth,
                "max_nodes": max_nodes,
                "truncated": truncated,
            },
        }

    def _wave_logic_value(
        self, wave: dict[str, Any], signal: str, raw_time: int
    ) -> dict[str, Any]:
        handle = wave["handle"]
        sig = self._wave_signal(wave, signal)
        if sig is None:
            return {"signal": signal, "ok": False, "error_code": "signal_not_dumped"}
        value = self._npi_call(
            "waveform.sig_hdl_value_at",
            self.waveform.sig_hdl_value_at,
            sig,
            raw_time,
            self.waveform.VctFormat_e.BinStrVal,
        )
        width = max(1, int(self._safe_handle_value(sig, "range_size", 1) or 1))
        return {
            "signal": signal,
            "ok": True,
            "value": {
                "kind": "logic",
                "width": width,
                "encoding": "bin",
                "value": str(value).lower(),
            },
        }

    def _record_control_samples(
        self, record: dict[str, Any], wave: dict[str, Any], raw_time: int
    ) -> list[dict[str, Any]]:
        candidates = []
        for summary in record.get("signals") or []:
            name = summary["ref"].get("full_name")
            if name and name not in candidates:
                candidates.append(name)
        return [
            self._wave_logic_value(wave, signal, raw_time)
            for signal in candidates[:64]
        ]

    def _simple_condition_value(
        self, text: str | None, samples: list[dict[str, Any]]
    ) -> bool | None:
        if not text or len(samples) != 1 or not samples[0].get("ok"):
            return None
        value = samples[0]["value"]["value"]
        if value not in {"0", "1"}:
            return None
        expression = re.sub(r"\s+", "", text)
        while expression.startswith("(") and expression.endswith(")"):
            expression = expression[1:-1]
        inverted = expression.startswith("!")
        if inverted:
            expression = expression[1:]
        signal = samples[0]["signal"]
        if expression not in {signal, signal.rsplit(".", 1)[-1]}:
            return None
        result = value == "1"
        return not result if inverted else result

    def trace_active_driver(self, args: dict[str, Any]) -> Any:
        wave_id, wave = self._wave(args.get("wave_id"))
        scale = self._handle_value(wave["handle"], "scale_unit")
        raw_time = to_raw_tick(args["time"], scale)
        max_nodes = max(1, int(args.get("max_nodes", 1000)))
        items = []
        for signal in args["signals"]:
            try:
                records = self._language_trace_records(signal, "driver")
            except StbError as exc:
                items.append({"signal": signal, "ok": False, "error_code": exc.code})
                continue
            controls_by_id = {}
            for record in records:
                statement = record.get("statement") or {}
                object_id = statement.get("ref", {}).get("object_id")
                if object_id and record.get("condition"):
                    samples = self._record_control_samples(record, wave, raw_time)
                    controls_by_id[object_id] = {
                        "record": record,
                        "samples": samples,
                        "value": self._simple_condition_value(
                            record["condition"].get("description"), samples
                        ),
                    }
            assignment_records = [
                record
                for record in records
                if (record.get("statement") or {}).get("ref", {}).get("npi_type")
                in {"npiAssignment", "npiContAssign"}
            ]
            branches = []
            for index, record in enumerate(assignment_records[:max_nodes]):
                evaluations = []
                for ancestor in record.get("ancestors") or []:
                    if ancestor["branch_role"] == "body":
                        continue
                    ancestor_id = ancestor["object"]["ref"].get("object_id")
                    control = controls_by_id.get(ancestor_id)
                    if control is None:
                        continue
                    condition_value = control["value"]
                    role = ancestor["branch_role"]
                    selected = (
                        None
                        if condition_value is None
                        else condition_value
                        if role != "false"
                        else not condition_value
                    )
                    evaluations.append(
                        {
                            "condition": control["record"]["condition"],
                            "branch_role": role,
                            "condition_value": condition_value,
                            "selected": selected,
                            "control_values": control["samples"],
                        }
                    )
                if any(item["selected"] is False for item in evaluations):
                    selection = "inactive"
                    reason = "runtime_condition_false"
                elif any(item["selected"] is None for item in evaluations):
                    selection = "indeterminate"
                    reason = "runtime_condition_unknown"
                else:
                    selection = "active"
                    reason = (
                        "runtime_conditions_true"
                        if evaluations
                        else "unconditional_assignment"
                    )
                branches.append(
                    {
                        "branch_id": f"b{index + 1}",
                        "selection": selection,
                        "reason": reason,
                        "record": record,
                        "condition_evaluations": evaluations,
                        "control_values": [
                            sample
                            for evaluation in evaluations
                            for sample in evaluation["control_values"]
                        ],
                    }
                )
            items.append(
                {
                    "signal": signal,
                    "ok": True,
                    "structural_driver_count": len(records),
                    "control_records": [
                        record
                        for record in records
                        if record not in assignment_records
                    ],
                    "branches": branches,
                    "truncated": len(assignment_records) > max_nodes,
                }
            )
        return {
            "wave_id": wave_id,
            "time": raw_time_point(raw_time, scale),
            "temporal_resolution": (
                "exact_sequence"
                if bool(self._handle_value(wave["handle"], "has_seq_num"))
                else "time_bucket"
            ),
            "layers": {
                "compile_time": {
                    "status": "preserved_by_loaded_design",
                    "evidence": "NPI elaborated Language Model",
                },
                "elaboration_time": {
                    "status": "preserved_by_loaded_design",
                    "evidence": "NPI elaborated hierarchy",
                },
                "runtime": {"status": "evaluated_when_controls_are_available"},
                "resolution": {"status": "structural_evidence_preserved"},
                "simulation_override": {
                    "status": "available"
                    if bool(self._handle_value(wave["handle"], "has_force_tag"))
                    else "not_recorded"
                },
            },
            "items": items,
        }

    def trace_value_origin(self, args: dict[str, Any]) -> Any:
        wave_id, wave = self._wave(args.get("wave_id"))
        scale = self._handle_value(wave["handle"], "scale_unit")
        raw_time = to_raw_tick(args["time"], scale)
        max_nodes = max(1, int(args.get("max_nodes", 1000)))
        items = []
        for signal in args["signals"]:
            sampled_value = self._wave_logic_value(wave, signal, raw_time)
            try:
                records = self._language_trace_records(signal, "driver")
            except StbError as exc:
                items.append({"signal": signal, "ok": False, "error_code": exc.code})
                continue
            event_controls = [
                record
                for record in records
                if (record.get("statement") or {}).get("ref", {}).get("npi_type")
                == "npiEventControl"
            ]
            assignments = [
                record
                for record in records
                if (record.get("statement") or {}).get("ref", {}).get("npi_type")
                in {"npiAssignment", "npiContAssign"}
            ]
            active_result = self.trace_active_driver(
                {
                    "signals": [signal],
                    "wave_id": wave_id,
                    "time": args["time"],
                    "max_nodes": max_nodes,
                }
            )
            active_branches = active_result["items"][0].get("branches") or []
            active_ids = {
                branch["record"]["statement"]["ref"]["object_id"]
                for branch in active_branches
                if branch["selection"] == "active"
            }
            selected_assignments = [
                record
                for record in assignments
                if record["statement"]["ref"]["object_id"] in active_ids
            ]
            latest_event = self._latest_sampling_event(
                wave, event_controls, raw_time
            )
            sample_time = (
                latest_event["raw_time"] if latest_event is not None else raw_time
            )
            hops = []
            for index, record in enumerate(selected_assignments[:max_nodes]):
                rhs = record.get("rhs")
                controls = self._record_control_samples(record, wave, sample_time)
                rhs_samples = [
                    sample
                    for sample in controls
                    if sample["signal"] != signal
                ]
                hops.append(
                    {
                        "hop_id": f"h{index + 1}",
                        "state_boundary": "sequential"
                        if event_controls
                        else "combinational_or_unknown",
                        "sampled_time": raw_time_point(sample_time, scale),
                        "assignment": record.get("statement"),
                        "assignment_source": rhs,
                        "sampling_event": latest_event["evidence"]
                        if latest_event
                        else None,
                        "control_values": controls,
                        "rhs_sampled_values": rhs_samples,
                        "temporal_resolution": (
                            "exact_sequence"
                            if bool(self._handle_value(wave["handle"], "has_seq_num"))
                            else "time_bucket"
                        ),
                        "stop_reason": (
                            "sampling_edge_not_found"
                            if event_controls and latest_event is None
                            else "structural_boundary"
                        ),
                    }
                )
            items.append(
                {
                    "signal": signal,
                    "ok": True,
                    "value": sampled_value,
                    "hops": hops,
                    "truncated": len(selected_assignments) > max_nodes,
                    "stop_reason": "no_language_driver" if not records else None,
                }
            )
        return {
            "wave_id": wave_id,
            "time": raw_time_point(raw_time, scale),
            "items": items,
        }

    def _latest_sampling_event(
        self,
        wave: dict[str, Any],
        event_records: list[dict[str, Any]],
        raw_time: int,
    ) -> dict[str, Any] | None:
        handle = wave["handle"]
        start = int(self._handle_value(handle, "min_time"))
        latest = None
        for record in event_records:
            condition = (record.get("condition") or {}).get("description") or ""
            names = {
                summary["ref"].get("full_name", "").rsplit(".", 1)[-1]: summary[
                    "ref"
                ].get("full_name")
                for summary in record.get("signals") or []
                if summary["ref"].get("full_name")
            }
            for edge, short_name in re.findall(
                r"\b(posedge|negedge)\s+([A-Za-z_][A-Za-z0-9_$]*)", condition
            ):
                signal = names.get(short_name)
                if not signal or self._wave_signal(wave, signal) is None:
                    continue
                changes = self._npi_call(
                    "waveform.sig_value_between",
                    lambda: list(
                        self.waveform.sig_value_between(
                            handle,
                            signal,
                            start,
                            raw_time,
                            self.waveform.VctFormat_e.BinStrVal,
                        )
                    ),
                )
                if changes is None:
                    changes = []
                previous = None
                for time_value, value in changes:
                    current = str(value).lower()
                    matched = (
                        edge == "posedge"
                        and previous == "0"
                        and current == "1"
                    ) or (
                        edge == "negedge"
                        and previous == "1"
                        and current == "0"
                    )
                    if matched and (
                        latest is None or int(time_value) > latest["raw_time"]
                    ):
                        latest = {
                            "raw_time": int(time_value),
                            "evidence": {
                                "event_control": record.get("statement"),
                                "edge": edge,
                                "signal": signal,
                                "condition": record.get("condition"),
                            },
                        }
                    previous = current
        return latest

    def _wave_summary(self, wave_id: str, obj: Any, kind: str) -> dict[str, Any]:
        return {
            "ref": {
                "model": "waveform",
                "context_id": self.context_id,
                "worker_generation": self.generation,
                "npi_type": kind,
                "full_name": self._handle_value(obj, "full_name"),
            },
            "name": self._handle_value(obj, "name"),
            "semantic_class": "waveform_scope" if kind == "SCOPE" else "waveform_signal",
            "classification_rule": "waveform_handle_type",
            "wave_id": wave_id,
        }

    def wave_value(self, args: dict[str, Any]) -> Any:
        wave_id, wave = self._wave(args.get("wave_id"))
        handle = wave["handle"]
        scale = self._handle_value(handle, "scale_unit")
        signals = list(args["signals"])
        results = []
        valid = [(signal, self._wave_signal(wave, signal)) for signal in signals]
        valid_handles = [sig for _, sig in valid if sig is not None]
        for time_spec in args["times"]:
            raw_time = to_raw_tick(time_spec, scale)
            values = self._npi_call(
                "waveform.sig_hdl_vec_value_at",
                self.waveform.sig_hdl_vec_value_at,
                valid_handles,
                raw_time,
                self.waveform.VctFormat_e.BinStrVal,
            )
            if values is None:
                values = [
                    self._npi_call(
                        "waveform.sig_hdl_value_at",
                        self.waveform.sig_hdl_value_at,
                        sig,
                        raw_time,
                        self.waveform.VctFormat_e.BinStrVal,
                    )
                    for sig in valid_handles
                ]
            value_iter = iter(values)
            for signal, sig in valid:
                if sig is None:
                    results.append(
                        {
                            "signal": signal,
                            "time": raw_time_point(raw_time, scale),
                            "ok": False,
                            "error_code": "signal_not_dumped",
                        }
                    )
                    continue
                value = next(value_iter)
                width = max(1, int(self._safe_handle_value(sig, "range_size", 1) or 1))
                results.append(
                    {
                        "signal": signal,
                        "time": raw_time_point(raw_time, scale),
                        "ok": True,
                        "value": {
                            "kind": "logic",
                            "width": width,
                            "encoding": "bin",
                            "value": str(value).lower(),
                        },
                    }
                )
        return {"wave_id": wave_id, "values": results}

    def wave_changes(self, args: dict[str, Any]) -> Any:
        wave_id, wave = self._wave(args.get("wave_id"))
        handle = wave["handle"]
        scale = self._handle_value(handle, "scale_unit")
        start = to_raw_tick(args["start"], scale)
        end = to_raw_tick(args["end"], scale)
        maximum = int(args.get("max_changes", 1000))
        direction = args.get("direction", "forward")
        if direction not in {"forward", "backward"}:
            raise StbError("invalid_request", f"invalid direction: {direction}")
        cursor_state = self._cursors.get(args.get("cursor"))
        request_key = {
            "context_id": self.context_id,
            "worker_generation": self.generation,
            "wave_id": wave_id,
            "wave_generation": wave["generation"],
            "signals": list(args["signals"]),
            "start": str(start),
            "end": str(end),
            "direction": direction,
        }
        cache_key = None
        if not args.get("cursor"):
            cache_key = json.dumps(
                {
                    "operation": "wave_changes",
                    "key": request_key,
                    "max_changes": maximum,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            cached = self._wave_changes_cache_get(cache_key)
            if cached is not None:
                return cached
        positions: dict[str, dict[str, int]] = {}
        if cursor_state is not None:
            if cursor_state.get("key") != request_key:
                raise StbError("cursor_mismatch", "cursor does not match request")
            positions = cursor_state.get("positions") or {}
        output = []
        has_more = False
        next_positions: dict[str, dict[str, int]] = {}
        returned = 0
        termination_reason = None
        for signal in args["signals"]:
            position = positions.get(signal) or {}
            if position.get("missing"):
                output.append(
                    {"signal": signal, "ok": False, "error_code": "signal_not_dumped"}
                )
                next_positions[signal] = {"missing": True}
                continue
            sig = self._wave_signal(wave, signal)
            if sig is None:
                output.append(
                    {"signal": signal, "ok": False, "error_code": "signal_not_dumped"}
                )
                next_positions[signal] = {"missing": True}
                continue
            if position.get("done"):
                output.append(
                    {
                        "signal": signal,
                        "ok": True,
                        "changes": [],
                        "truncated": False,
                    }
                )
                next_positions[signal] = {"done": True}
                continue
            page, page_soft_timeout = self._bounded_wave_changes(
                handle,
                sig,
                start,
                end,
                direction,
                maximum + 1,
                position,
            )
            clipped = page[:maximum]
            signal_has_more = len(page) > maximum or page_soft_timeout
            if page_soft_timeout:
                termination_reason = "soft_timeout"
            has_more = has_more or signal_has_more
            returned += len(clipped)
            if signal_has_more and clipped:
                last_time = clipped[-1][0]
                same_time_count = sum(time == last_time for time, _ in clipped)
                previous = position
                if previous and int(previous["time"]) == last_time:
                    same_time_count += int(previous.get("count", 0))
                next_positions[signal] = {
                    "time": last_time,
                    "count": same_time_count,
                }
            elif not signal_has_more:
                next_positions[signal] = {"done": True}
            width = max(1, int(self._safe_handle_value(sig, "range_size", 1) or 1))
            output.append(
                {
                    "signal": signal,
                    "ok": True,
                    "changes": [
                        {
                            "time": raw_time_point(time, scale),
                            "value": {
                                "kind": "logic",
                                "width": width,
                                "encoding": "bin",
                                "value": str(value).lower(),
                            },
                        }
                        for time, value in clipped
                    ],
                    "truncated": signal_has_more,
                }
            )
        next_cursor = (
            self._cursors.issue({"key": request_key, "positions": next_positions})
            if has_more
            else None
        )
        cursor_state = (
            {"key": request_key, "positions": next_positions} if has_more else None
        )
        result = {
            "wave_id": wave_id,
            "signals": output,
            "direction": direction,
            "next_cursor": next_cursor,
            "truncated": has_more,
            "termination_reason": termination_reason
            or ("transition_limit" if has_more else None),
            "returned": returned,
            "scanned": returned,
        }
        return (
            self._wave_changes_cache_put(cache_key, result, cursor_state)
            if cache_key
            else result
        )

    def _bounded_wave_changes(
        self,
        file_handle: Any,
        signal_handle: Any,
        start: int,
        end: int,
        direction: str,
        maximum: int,
        position: dict[str, int] | None = None,
    ) -> tuple[list[tuple[int, Any]], bool]:
        vct = None
        rows: list[tuple[int, Any]] = []
        soft_timeout = False
        try:
            self._npi_call("waveform.file.reset_sig_list", file_handle.reset_sig_list)
            if not self._npi_call(
                "waveform.file.add_to_sig_list",
                file_handle.add_to_sig_list,
                signal_handle,
            ):
                raise StbError("wave_read_failed", "failed to select waveform signal")
            if not self._npi_call(
                "waveform.file.load_vc_by_range",
                file_handle.load_vc_by_range,
                start,
                end,
            ):
                raise StbError("wave_read_failed", "failed to load waveform range")
            vct = self._npi_call(
                "waveform.signal.create_vct",
                signal_handle.create_vct,
            )
            if vct is None:
                raise StbError("wave_read_failed", "failed to create value-change cursor")
            if direction == "forward":
                valid = bool(
                    self._npi_call(
                        "waveform.vct.goto_time",
                        vct.goto_time,
                        int(position["time"]),
                    )
                    if position
                    else self._npi_call("waveform.vct.goto_first", vct.goto_first)
                )
                step = vct.goto_next
            else:
                valid = bool(
                    self._npi_call(
                        "waveform.vct.goto_time",
                        vct.goto_time,
                        int(position["time"]),
                    )
                    if position
                    else self._npi_call("waveform.vct.goto_time", vct.goto_time, end)
                )
                step = vct.goto_prev
            skipped_at_position = 0
            while valid and len(rows) < maximum:
                if rows and self.soft_timed_out():
                    soft_timeout = True
                    break
                span_started = time.perf_counter()
                npi_calls = 1
                raw_time = int(vct.time())
                if position and raw_time == int(position["time"]):
                    skipped_at_position += 1
                    if skipped_at_position <= int(position.get("count", 0)):
                        npi_calls += 1
                        valid = bool(step())
                        self._record_npi_elapsed(span_started, npi_calls)
                        continue
                if start <= raw_time <= end:
                    npi_calls += 1
                    value = vct.value(self.waveform.VctFormat_e.BinStrVal)
                    rows.append(
                        (
                            raw_time,
                            value,
                        )
                    )
                elif direction == "forward" and raw_time > end:
                    self._record_npi_elapsed(span_started, npi_calls)
                    break
                elif direction == "backward" and raw_time < start:
                    self._record_npi_elapsed(span_started, npi_calls)
                    break
                npi_calls += 1
                valid = bool(step())
                self._record_npi_elapsed(span_started, npi_calls)
            return rows, soft_timeout
        finally:
            if vct is not None:
                try:
                    self._npi_call("waveform.vct.release", vct.release)
                except Exception:
                    pass
            try:
                self._npi_call("waveform.file.unload_vc", file_handle.unload_vc)
            except Exception:
                pass
            try:
                self._npi_call("waveform.file.reset_sig_list", file_handle.reset_sig_list)
            except Exception:
                pass

    def wave_compute(self, args: dict[str, Any]) -> Any:
        operation = args["operation"]
        if operation == "sample":
            return self.wave_value(args)
        if operation in {"statistics", "xz", "period", "pulse", "find"}:
            wave_id, wave = self._wave(args.get("wave_id"))
            handle = wave["handle"]
            scale = self._handle_value(handle, "scale_unit")
            start = to_raw_tick(args["start"], scale)
            end = to_raw_tick(args["end"], scale)
            results = []
            for signal in args["signals"]:
                sig = self._wave_signal(wave, signal)
                if sig is None:
                    results.append(
                        {"signal": signal, "ok": False, "error_code": "signal_not_dumped"}
                    )
                    continue
                changes = self._npi_call(
                    "waveform.sig_value_between",
                    lambda: list(
                        self.waveform.sig_value_between(
                            handle,
                            signal,
                            start,
                            end,
                            self.waveform.VctFormat_e.BinStrVal,
                        )
                    ),
                )
                if changes is None:
                    changes = []
                values = [str(value).lower() for _, value in changes]
                if operation == "statistics":
                    data = {
                        "transition_count": max(0, len(changes) - 1),
                        "sample_count": len(changes),
                        "xz_event_count": sum(
                            any(bit in value for bit in "xz") for value in values
                        ),
                        "first_time": raw_time_point(changes[0][0], scale)
                        if changes
                        else None,
                        "last_time": raw_time_point(changes[-1][0], scale)
                        if changes
                        else None,
                    }
                elif operation == "xz":
                    matches = [
                        {
                            "time": raw_time_point(time, scale),
                            "value": value,
                        }
                        for (time, _), value in zip(changes, values)
                        if any(bit in value for bit in "xz")
                    ]
                    data = {
                        "matches": matches[: int(args.get("max_matches", 1000))],
                        "truncated": len(matches) > int(args.get("max_matches", 1000)),
                    }
                elif operation == "find":
                    target = str(args["value"]).lower()
                    matches = [
                        {"time": raw_time_point(time, scale), "value": value}
                        for (time, _), value in zip(changes, values)
                        if value == target
                    ]
                    data = {
                        "matches": matches[: int(args.get("max_matches", 1000))],
                        "truncated": len(matches) > int(args.get("max_matches", 1000)),
                    }
                else:
                    edge = args.get("edge", "posedge")
                    edge_times = []
                    for index in range(1, len(changes)):
                        previous = values[index - 1]
                        current = values[index]
                        if edge == "posedge" and previous == "0" and current == "1":
                            edge_times.append(changes[index][0])
                        elif edge == "negedge" and previous == "1" and current == "0":
                            edge_times.append(changes[index][0])
                    if operation == "period":
                        intervals = [
                            edge_times[index] - edge_times[index - 1]
                            for index in range(1, len(edge_times))
                        ]
                        data = {
                            "edge": edge,
                            "edge_count": len(edge_times),
                            "period_ticks": [str(value) for value in intervals],
                            "scale_unit": scale,
                        }
                    else:
                        pulses = []
                        for index in range(1, len(changes)):
                            if values[index - 1] != values[index]:
                                pulses.append(
                                    {
                                        "value": values[index - 1],
                                        "start": raw_time_point(changes[index - 1][0], scale),
                                        "end": raw_time_point(changes[index][0], scale),
                                        "duration_raw_ticks": str(
                                            changes[index][0] - changes[index - 1][0]
                                        ),
                                    }
                                )
                        data = {"pulses": pulses}
                results.append({"signal": signal, "ok": True, "data": data})
            return {"wave_id": wave_id, "operation": operation, "items": results}
        if operation in {"evaluate_window", "extract_events", "match_transactions"}:
            return self._wave_compute_ir(args)
        if operation in {"compare", "first_divergence"}:
            left_id, left = self._wave(args["left"]["wave_id"])
            right_id, right = self._wave(args["right"]["wave_id"])
            left_signal = args["left"]["signal"]
            right_signal = args["right"]["signal"]
            if operation == "compare":
                times = args["times"]
            else:
                left_scale = self._handle_value(left["handle"], "scale_unit")
                right_scale = self._handle_value(right["handle"], "scale_unit")
                if left_scale != right_scale:
                    raise StbError(
                        "unsupported_capability",
                        "first_divergence currently requires equal FSDB scales",
                    )
                start = to_raw_tick(args["start"], left_scale)
                end = to_raw_tick(args["end"], left_scale)
                left_changes = self._npi_call(
                    "waveform.sig_value_between",
                    lambda: list(
                        self.waveform.sig_value_between(
                            left["handle"], left_signal, start, end
                        )
                    ),
                )
                right_changes = self._npi_call(
                    "waveform.sig_value_between",
                    lambda: list(
                        self.waveform.sig_value_between(
                            right["handle"], right_signal, start, end
                        )
                    ),
                )
                raw_times = sorted(
                    {start, end}
                    | {item[0] for item in left_changes}
                    | {item[0] for item in right_changes}
                )
                times = [
                    f"{time * self._scale_multiplier(left_scale)}"
                    f"{self._scale_base_unit(left_scale)}"
                    for time in raw_times
                ]
            rows = []
            for time_spec in times:
                left_tick = to_raw_tick(
                    time_spec, self._handle_value(left["handle"], "scale_unit")
                )
                right_tick = to_raw_tick(
                    time_spec, self._handle_value(right["handle"], "scale_unit")
                )
                left_value = self._npi_call(
                    "waveform.sig_value_at",
                    self.waveform.sig_value_at,
                    left["handle"],
                    left_signal,
                    left_tick,
                )
                right_value = self._npi_call(
                    "waveform.sig_value_at",
                    self.waveform.sig_value_at,
                    right["handle"],
                    right_signal,
                    right_tick,
                )
                equal = left_value == right_value
                rows.append(
                    {
                        "time": time_spec,
                        "left": str(left_value).lower(),
                        "right": str(right_value).lower(),
                        "equal": equal,
                    }
                )
                if operation == "first_divergence" and not equal:
                    return {
                        "operation": operation,
                        "left_wave_id": left_id,
                        "right_wave_id": right_id,
                        "divergence": rows[-1],
                    }
            return {
                "operation": operation,
                "left_wave_id": left_id,
                "right_wave_id": right_id,
                "rows": rows,
                "divergence": None,
            }
        raise StbError("unsupported_operation", f"wave operation not implemented: {operation}")

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
            if any(any(bit in value for bit in "xz") for value in args):
                result = "x"
            else:
                result = "1" if args[0] == args[1] else "0"
            return (
                "1" if result == "0" else "0" if result == "1" else "x"
            ) if op == "logic.ne" else result
        if op in {"logic.and", "logic.or"}:
            truths = [self._logic_truth(value) for value in args]
            if op == "logic.and":
                return "0" if False in truths else "x" if None in truths else "1"
            return "1" if True in truths else "x" if None in truths else "0"
        if op == "logic.not":
            truth = self._logic_truth(args[0])
            return "x" if truth is None else "0" if truth else "1"
        if op == "logic.is_known":
            return "0" if any(bit in args[0] for bit in "xz") else "1"
        if op == "logic.is_x":
            return "1" if "x" in args[0] else "0"
        if op == "logic.is_z":
            return "1" if "z" in args[0] else "0"
        if op == "bit.not":
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
        handle = wave["handle"]
        scale = self._handle_value(handle, "scale_unit")
        raw_times = {start, end}
        for signal in signals:
            if self._wave_signal(wave, signal) is None:
                raise StbError("signal_not_dumped", f"signal not dumped: {signal}")
            raw_times.update(
                time
                for time, _ in (
                    self._npi_call(
                        "waveform.sig_value_between",
                        lambda: list(
                            self.waveform.sig_value_between(
                                handle,
                                signal,
                                start,
                                end,
                                self.waveform.VctFormat_e.BinStrVal,
                            )
                        ),
                    )
                    or []
                )
            )
        ordered = sorted(raw_times)
        truncated = len(ordered) > max_points
        rows = []
        for raw_time in ordered[:max_points]:
            values = {
                signal: str(
                    self._npi_call(
                        "waveform.sig_value_at",
                        self.waveform.sig_value_at,
                        handle,
                        signal,
                        raw_time,
                    )
                ).lower()
                for signal in signals
            }
            result = self._eval_expr(root, values)
            rows.append(
                {
                    "time": raw_time_point(raw_time, scale),
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
        scale = self._handle_value(wave["handle"], "scale_unit")
        start = to_raw_tick(args["start"], scale)
        end = to_raw_tick(args["end"], scale)
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
                while (
                    end_index < len(ends)
                    and int(ends[end_index]["time"]["raw_ticks"]) < start_tick
                ):
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

    def _scale_multiplier(self, scale: str) -> int:
        from stb.timeutil import parse_scale_unit

        return parse_scale_unit(scale)[0]

    def _scale_base_unit(self, scale: str) -> str:
        from stb.timeutil import parse_scale_unit

        return parse_scale_unit(scale)[1]

    def source_context(self, args: dict[str, Any]) -> Any:
        ref = args["reference"]
        self.validate_ref(ref)
        if ref.get("model") == "waveform":
            raise StbError("source_unavailable", "waveform objects have no source context")
        if ref.get("model") == "language":
            obj = self._resolve_language_ref(ref)
            source = self._language_source(obj)
        else:
            obj = self._resolve_design(ref["full_name"], ref.get("npi_type"))
            source = self._source(obj)
        if source is None:
            raise StbError("source_unavailable", "NPI object has no source location")
        reported_file = source["file"]
        path = self._resolve_source_path(reported_file)
        if not path.is_file():
            raise StbError(
                "source_unavailable",
                f"source file not found: {reported_file}",
                {"reported_file": reported_file, "resolved_candidate": str(path)},
            )
        self._assert_allowed_path(path)
        source = {
            **source,
            "file": str(path),
            "reported_file": reported_file,
        }
        current_fingerprint = self._fingerprint(path)
        previous_fingerprint = self._source_fingerprints.get(str(path))
        if previous_fingerprint is None:
            try:
                source_mtime_ns = path.stat().st_mtime_ns
            except OSError as exc:
                raise StbError("source_unavailable", f"source file not found: {path}") from exc
            if source_mtime_ns > self._context_open_time_ns:
                previous_fingerprint = "unknown_at_context_open"
            else:
                previous_fingerprint = current_fingerprint
                self._source_fingerprints[str(path)] = current_fingerprint
        before = max(0, int(args.get("before_lines", 2)))
        after = max(0, int(args.get("after_lines", 2)))
        max_lines = max(1, int(args.get("max_lines", 20)))
        max_chars = max(1, int(args.get("max_chars", 8000)))
        start = max(1, source["begin_line"] - before)
        snapshot_key = "|".join(
            str(value)
            for value in (
                path,
                source["begin_line"],
                source["end_line"],
                before,
                after,
                max_lines,
                max_chars,
            )
        )
        changed = current_fingerprint != previous_fingerprint
        snapshot = self._source_snapshots.get(snapshot_key)
        use_current = not changed or bool(args.get("allow_current_changed_source", False))
        if changed and not use_current and snapshot is None:
            raise StbError(
                "source_changed",
                "source changed and no retained snapshot is available",
                {
                    "resource": str(path),
                    "loaded_fingerprint": previous_fingerprint,
                    "current_fingerprint": current_fingerprint,
                },
            )
        if use_current:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            end = min(len(lines), source["end_line"] + after, start + max_lines - 1)
            selected = "\n".join(
                f"{line_no}: {lines[line_no - 1]}"
                for line_no in range(start, end + 1)
            )
            truncated = len(selected) > max_chars
            if not changed:
                snapshot = {
                    "start_line": start,
                    "end_line": end,
                    "text": selected[:max_chars],
                    "truncated": truncated,
                    "fingerprint": current_fingerprint,
                }
                self._source_snapshots[snapshot_key] = snapshot
        else:
            assert snapshot is not None
            start = int(snapshot["start_line"])
            end = int(snapshot["end_line"])
            selected = str(snapshot["text"])
            truncated = bool(snapshot["truncated"])
        include_preprocessor = bool(args.get("include_preprocessor", False))
        preprocessor_evidence = (
            self._text_evidence(
                path,
                start,
                end,
                reported_file=reported_file,
            )
            if include_preprocessor
            else {
                "available": False,
                "reason": "not_requested",
                "macros": [],
                "includes": [],
            }
        )
        expansion_context = self._expansion_context(
            path,
            source,
            preprocessor_evidence,
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
            "source": source,
            "start_line": start,
            "end_line": end,
            "text": selected[:max_chars],
            "truncated": truncated,
            "fingerprint": current_fingerprint if use_current else previous_fingerprint,
            "current_fingerprint": current_fingerprint,
            "loaded_fingerprint": previous_fingerprint,
            "change_status": "changed" if changed else "unchanged",
            "source_alignment": "stale" if changed else "aligned",
            "source_variant": "current" if use_current else "retained_snapshot",
            "expansion_context_id": expansion_context["expansion_context_id"],
            "expansion_context": expansion_context,
            "preprocessor_evidence": preprocessor_evidence,
        }

    def assertion_structure(self, args: dict[str, Any]) -> Any:
        ref = args["reference"]
        self.validate_ref(ref)
        if ref.get("model") != "language":
            raise StbError(
                "unsupported_capability",
                "assertion_structure requires a language ObjectRef",
            )
        capability = self._assertion_capability()
        if capability.get("status") != "available":
            raise StbError(
                "unsupported_capability",
                "assertion object discovery and source anchoring are unavailable",
                capability,
            )
        handle = self._resolve_language_ref(ref)
        assertion_type = self._language_type(handle)
        if assertion_type != "npiAssert":
            raise StbError(
                "unsupported_capability",
                "assertion_structure V1 supports only npiAssert property statements",
                {"actual_type": assertion_type},
            )

        max_lines = int(args.get("max_source_lines", 200))
        max_chars = int(args.get("max_chars", 65_536))
        source_args = {
            "reference": ref,
            "before_lines": 0,
            "after_lines": max_lines,
            "max_lines": max_lines,
            "max_chars": max_chars,
            "include_preprocessor": bool(args.get("include_preprocessor", True)),
            "allow_current_changed_source": bool(
                args.get("allow_current_changed_source", False)
            ),
        }
        assertion_source = self.source_context(source_args)
        property_summary = None
        property_source = None
        property_type = None
        property_handle = None
        try:
            relation = getattr(handle, "property_handle", None)
            property_handle = (
                self._npi_call("lang.assertion.property_handle", relation)
                if relation is not None
                else None
            )
        except Exception:
            property_handle = None
        property_decl_handle = None
        if property_handle is not None:
            property_type = self._language_type(property_handle)
            if property_type == "npiPropertyInst":
                relation = getattr(property_handle, "property_decl_handle", None)
                try:
                    property_decl_handle = (
                        self._npi_call(
                            "lang.property_inst.property_decl_handle",
                            relation,
                        )
                        if relation is not None
                        else None
                    )
                except Exception:
                    property_decl_handle = None
        if property_decl_handle is not None:
            property_summary = self._language_summary(property_decl_handle)
            property_source = self.source_context(
                {
                    **source_args,
                    "reference": property_summary["ref"],
                }
            )

        assertion_text = strip_numbered_source(assertion_source["text"])
        property_text = (
            strip_numbered_source(property_source["text"])
            if property_source is not None
            else None
        )
        structure = parse_assertion_source(
            assertion_text,
            property_source=property_text,
        )
        assertion_summary = self._language_summary(handle)
        assertion_scope = (
            str(assertion_summary["ref"].get("full_name") or "").rsplit(".", 1)[0]
        )

        def resolve_identifier(token: str) -> dict[str, Any] | None:
            candidates = [token]
            if assertion_scope and not token.startswith(assertion_scope + "."):
                candidates.insert(0, f"{assertion_scope}.{token}")
            for candidate in dict.fromkeys(candidates):
                try:
                    resolved = self._npi_call(
                        "lang.handle_by_name",
                        self.lang.handle_by_name,
                        candidate,
                        None,
                    )
                except Exception:
                    resolved = None
                if resolved is not None:
                    return self._language_summary(resolved)
            return None

        structure = resolve_structure_dependencies(structure, resolve_identifier)
        assertion_source["declaration_text"] = structure.get("assertion", {}).get(
            "raw"
        )
        if property_source is not None:
            property_source["declaration_text"] = structure.get(
                "property_declaration", {}
            ).get("raw")
        truncated = bool(assertion_source.get("truncated")) or bool(
            property_source and property_source.get("truncated")
        )
        result = {
            "schema_version": "stb.assertion-structure.v1",
            "capability": capability,
            "anchor": assertion_summary,
            "source_evidence": assertion_source,
            "property_anchor": property_summary,
            "property_source_evidence": property_source,
            "npi_cross_reference": {
                "assertion_type": assertion_type,
                "property_type": property_type,
                "property_declaration": property_summary["ref"]
                if property_summary
                else None,
            },
            "structure": structure,
            "truncated": truncated,
        }
        if property_handle is not None and property_type == "npiPropertyInst":
            self._retain_language(property_handle)
        elif property_handle is not None:
            try:
                self._npi_call(
                    "lang.release_handle",
                    self.lang.release_handle,
                    property_handle,
                )
            except Exception:
                pass
        return result

    def _expansion_context(
        self,
        path: Path,
        source: dict[str, Any],
        preprocessor: dict[str, Any],
    ) -> dict[str, Any]:
        include_chain = source.get("include_chain") or []
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
            "physical_file": str(path),
            "include_site": {
                "file": source.get("reported_file") or str(path),
                "line": source.get("begin_line"),
            },
            "include_chain": include_chain,
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
                "status": "bounded_relevant"
                if preprocessor.get("available")
                else "unavailable",
                "complete": False,
                "reason": (
                    "complete macro environment is not exposed by the reviewed "
                    "SP1 Text NPI adapter"
                )
                if preprocessor.get("available")
                else preprocessor.get("reason", "not_available"),
            },
        }

    def _text_evidence(
        self,
        path: Path,
        start_line: int,
        end_line: int,
        reported_file: str | None = None,
    ) -> dict[str, Any]:
        file_handle = None
        names = [reported_file, str(path), path.name]
        for name in dict.fromkeys(item for item in names if item):
            try:
                file_handle = self._npi_call(
                    "text.file_by_name",
                    self.text.file_by_name,
                    name,
                )
            except Exception:
                file_handle = None
            if file_handle is not None:
                break
        if file_handle is None and not self._precompiled_db:
            try:
                for candidate in (
                    self._npi_call("text.get_file_list", self.text.get_file_list)
                    or []
                ):
                    full_name = Path(
                        self._handle_value(candidate, "file_full_name")
                    ).resolve()
                    if full_name == path:
                        file_handle = candidate
                        break
            except Exception:
                file_handle = None
        if file_handle is None:
            return {"available": False, "macros": [], "includes": []}
        macros = []
        includes = []
        for line_number in range(start_line, end_line + 1):
            try:
                line = self._npi_call(
                    "text.file.line_by_number",
                    file_handle.line_by_number,
                    line_number,
                )
                words = (
                    self._npi_call("text.line.word_handles", line.word_handles)
                    if line is not None
                    else []
                )
            except Exception:
                continue
            for word in words or []:
                try:
                    attribute = self._npi_call(
                        "text.word.word_attribute",
                        word.word_attribute,
                    )
                    name = self._npi_call("text.word.word_name", word.word_name)
                except Exception:
                    continue
                if attribute == "npiTextMacroName":
                    try:
                        macro = self._npi_call(
                            "text.word.text_macro",
                            word.text_macro,
                        )
                        arg_num = int(
                            self._npi_call("text.macro.arg_num", macro.arg_num)
                        )
                        macros.append(
                            {
                                "name": name,
                                "line": line_number,
                                "expanded_value": str(
                                    self._npi_call("text.macro.value", macro.value)
                                )[:2000],
                                "definition": {
                                    "file": self._npi_call(
                                        "text.macro.def_file", macro.def_file
                                    ),
                                    "line": int(
                                        self._npi_call(
                                            "text.macro.def_line", macro.def_line
                                        )
                                    ),
                                    "value": str(
                                        self._npi_call(
                                            "text.macro.def_value", macro.def_value
                                        )
                                    )[:2000],
                                    "arguments": [
                                        self._npi_call(
                                            "text.macro.def_arg",
                                            macro.def_arg,
                                            index,
                                        )
                                        for index in range(arg_num)
                                    ],
                                },
                                "arguments": [
                                    self._npi_call(
                                        "text.macro.arg",
                                        macro.arg,
                                        index,
                                    )
                                    for index in range(arg_num)
                                ],
                            }
                        )
                    except Exception:
                        macros.append(
                            {
                                "name": name,
                                "line": line_number,
                                "status": "metadata_unavailable",
                            }
                        )
                try:
                    included = self._npi_call(
                        "text.word.file_by_include_word",
                        word.file_by_include_word,
                    )
                except Exception:
                    included = None
                if included is not None:
                    try:
                        include_path = str(
                            Path(
                                self._handle_value(included, "file_full_name")
                            ).resolve()
                        )
                    except Exception:
                        include_path = self._handle_value(included, "file_name")
                    includes.append(
                        {
                            "token": name,
                            "line": line_number,
                            "file": include_path,
                        }
                    )
        return {
            "available": True,
            "file_type": path.suffix.lower(),
            "macros": macros,
            "includes": includes,
            "note": "read-only Text NPI metadata; no expand/mutation API invoked",
        }

    def _mapping_cache_get(self, key: str) -> dict[str, Any] | None:
        cached = self._mapping_cache.get(key)
        if cached is None:
            self._request_metrics["cache_misses"] += 1
            return None
        self._request_metrics["cache_hits"] += 1
        self._mapping_cache.move_to_end(key)
        return cached

    def _mapping_cache_put(self, key: str, value: dict[str, Any]) -> None:
        self._mapping_cache[key] = value
        self._mapping_cache.move_to_end(key)
        while len(self._mapping_cache) > 1024:
            self._mapping_cache.popitem(last=False)

    def _actual_name_candidates(self, design_name: str) -> tuple[list[str], dict[str, Any]]:
        try:
            design_obj = self._resolve_design(design_name, None)
        except StbError:
            return [], {
                "status": "unavailable",
                "reason": "design object was not resolved for actual-name evidence",
            }
        candidates = []
        for method_name in (
            "actual_name",
            "actual_full_name",
            "actual_net_name",
            "actual_net_full_name",
        ):
            value = self._safe_handle_value(design_obj, method_name)
            if value:
                candidates.append(str(value))
        if not candidates:
            return [], {
                "status": "unavailable",
                "reason": "reviewed SP1 adapter has no stable actual-name result",
            }
        return list(dict.fromkeys(candidates)), {
            "status": "available",
            "methods": [
                "actual_name",
                "actual_full_name",
                "actual_net_name",
                "actual_net_full_name",
            ],
        }

    def _mapping_range(self, handle: Any) -> dict[str, Any]:
        left = self._safe_handle_value(handle, "left_range")
        right = self._safe_handle_value(handle, "right_range")
        size = self._safe_handle_value(handle, "range_size")
        direction = "unknown"
        if isinstance(left, int) and isinstance(right, int):
            direction = "descending" if left > right else "ascending"
        return {
            "left": left,
            "right": right,
            "size": size,
            "direction": direction,
        }

    def mapping(self, args: dict[str, Any]) -> Any:
        action = args["action"]
        if action in {"resolve", "explain"}:
            design_name = args["design_full_name"]
            wave_id, wave = self._wave(args.get("wave_id"))
            cache_key = json.dumps(
                {
                    "operation": "mapping",
                    "context_id": self.context_id,
                    "worker_generation": self.generation,
                    "wave_id": wave_id,
                    "wave_generation": wave["generation"],
                    "args": args,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            cached = self._mapping_cache_get(cache_key)
            if cached is not None:
                if not cached["ok"]:
                    error = cached["error"]
                    raise StbError(
                        error["code"],
                        error["message"],
                        error.get("details"),
                        error.get("recoverable", True),
                    )
                result = dict(cached["data"])
                result["cache"] = "hit"
                return result
            candidates: list[tuple[str, str]] = []
            pipeline = []
            context_mode = args.get("context_mode", "same")
            if context_mode == "same":
                candidates.append((design_name, "full_name"))
                actual_candidates, actual_evidence = self._actual_name_candidates(
                    design_name
                )
                for candidate in actual_candidates:
                    candidates.append((candidate, "actual_name"))
                normalized = design_name.replace("/", ".")
                if normalized != design_name:
                    candidates.append((normalized, "identifier_normalization"))
                range_normalized = re.sub(r"\[(\d+):(\d+)\]$", "", design_name)
                if range_normalized != design_name:
                    candidates.append(
                        (range_normalized, "bus_range_normalization")
                    )
                bit_blasted = re.sub(r"\[(\d+)\]$", "", design_name)
                if bit_blasted != design_name:
                    candidates.append((bit_blasted, "bit_blasting"))
            else:
                actual_evidence = {
                    "status": "unavailable",
                    "reason": "cross-context mapping uses explicit profile rules",
                }
                profile = args.get("profile") or {}
                for rule in profile.get("rules") or []:
                    kind = rule.get("kind")
                    if kind == "exact":
                        pairs = rule.get("pairs") or {}
                        if design_name in pairs:
                            candidates.append((str(pairs[design_name]), "profile.exact"))
                    elif kind == "prefix_replace":
                        source = str(rule.get("source_prefix", ""))
                        target = str(rule.get("target_prefix", ""))
                        if design_name.startswith(source):
                            candidates.append(
                                (
                                    target + design_name[len(source) :],
                                    "profile.prefix_replace",
                                )
                            )
                    elif kind == "separator_normalize":
                        source = str(rule.get("source", "/"))
                        target = str(rule.get("target", "."))
                        candidates.append(
                            (
                                design_name.replace(source, target),
                                "profile.separator_normalize",
                            )
                        )
                    elif kind == "regex_replace":
                        pattern = str(rule.get("pattern", ""))
                        replacement = str(rule.get("replacement", ""))
                        candidates.append(
                            (
                                re.sub(pattern, replacement, design_name),
                                "profile.regex_replace",
                            )
                        )
                    elif kind == "bit_mapping":
                        for pair in rule.get("pairs") or []:
                            if pair.get("design") == design_name:
                                candidates.append(
                                    (str(pair.get("waveform")), "profile.bit_mapping")
                                )
                if not candidates:
                    raise StbError(
                        "mapping_profile_required",
                        "cross-context mapping requires a matching explicit rule",
                    )
            candidates = list(dict.fromkeys(candidates))
            matches = []
            for candidate, rule_name in candidates:
                signal = self._wave_signal(wave, candidate)
                pipeline.append(
                    {
                        "rule": rule_name,
                        "candidate": candidate,
                        "status": "matched" if signal is not None else "not_found",
                    }
                )
                if signal is not None:
                    matches.append((candidate, rule_name, signal))
            unique_matches: dict[str, tuple[str, str, Any]] = {}
            for candidate, rule_name, signal in matches:
                unique_matches[str(self._handle_value(signal, "full_name"))] = (
                    candidate,
                    rule_name,
                    signal,
                )
            if len(unique_matches) > 1:
                error = StbError(
                    "ambiguous_mapping",
                    "multiple deterministic mapping candidates matched",
                    {"matches": sorted(unique_matches)},
                )
                self._mapping_cache_put(
                    cache_key, {"ok": False, "error": error.as_dict()}
                )
                raise error
            if not unique_matches:
                result = {
                    "action": action,
                    "context_mode": context_mode,
                    "wave_id": wave_id,
                    "wave_generation": wave["generation"],
                    "design_full_name": design_name,
                    "pipeline": pipeline,
                    "actual_name_evidence": actual_evidence,
                    "select_concat_slice_evidence": {
                        "status": "unavailable",
                        "reason": (
                            "reviewed SP1 adapter does not expose a stable public "
                            "V1 resolver"
                        ),
                    },
                    "cache": "miss",
                }
                if action == "explain":
                    self._mapping_cache_put(cache_key, {"ok": True, "data": result})
                    return result
                error = StbError("signal_not_dumped", f"signal not dumped: {design_name}")
                self._mapping_cache_put(
                    cache_key, {"ok": False, "error": error.as_dict()}
                )
                raise error
            candidate, rule_name, signal = next(iter(unique_matches.values()))
            design_obj = None
            try:
                design_obj = self._resolve_design(design_name, None)
            except StbError:
                pass
            result = {
                "action": action,
                "context_mode": context_mode,
                "wave_id": wave_id,
                "wave_generation": wave["generation"],
                "design_full_name": design_name,
                "waveform_full_name": self._handle_value(signal, "full_name"),
                "rule": rule_name,
                "selected_candidate": candidate,
                "pipeline": pipeline,
                "actual_name_evidence": actual_evidence,
                "select_concat_slice_evidence": {
                    "status": "unavailable",
                    "reason": "reviewed SP1 adapter does not expose a stable public V1 resolver",
                },
                "bit_mapping": {
                    "kind": "identity",
                    "design_range": self._mapping_range(design_obj)
                    if design_obj is not None
                    else None,
                    "waveform_range": self._mapping_range(signal),
                    "direction": "preserved",
                },
                "cache": "miss",
            }
            self._mapping_cache_put(cache_key, {"ok": True, "data": result})
            return result
        if action == "validate":
            profile = args.get("profile") or {}
            allowed = {
                "exact",
                "prefix_replace",
                "separator_normalize",
                "regex_replace",
                "bit_mapping",
            }
            rules = profile.get("rules") or []
            invalid = [rule for rule in rules if rule.get("kind") not in allowed]
            if invalid:
                raise StbError("invalid_request", "mapping profile has invalid rules")
            return {"valid": True, "rule_count": len(rules)}
        raise StbError("unsupported_operation", f"mapping action not implemented: {action}")
