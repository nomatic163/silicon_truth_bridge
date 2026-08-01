from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType


VERIFIED_VERDI_RELEASES = frozenset({"V-2023.12-SP1"})

_RELEASE_PATTERN = re.compile(r"^[A-Z]-\d{4}\.\d{2}(?:-[A-Za-z0-9.]+)*$")

REQUIRED_NPI_SYMBOLS = {
    "npisys": ("init", "end", "load_design"),
    "netlist": ("ObjectType", "get_inst", "get_top_inst_list"),
    "lang": (
        "expr_decompile",
        "get_hdl_info",
        "handle_by_name",
        "release_handle",
        "trace_driver2",
        "trace_load2",
    ),
    "text": ("file_by_name", "get_file_list"),
    "waveform": (
        "VctFormat_e",
        "close",
        "open",
        "sig_hdl_value_at",
        "sig_hdl_vec_value_at",
        "sig_value_at",
        "sig_value_between",
    ),
}


def detect_verdi_release(verdi_home: Path, override: str | None = None) -> str:
    if override:
        return override

    candidates = [verdi_home]
    try:
        candidates.insert(0, verdi_home.resolve())
    except OSError:
        pass

    for candidate in candidates:
        for part in reversed(candidate.parts):
            if _RELEASE_PATTERN.fullmatch(part):
                return part
    return verdi_home.name or "unknown"


def missing_npi_symbols(modules: dict[str, ModuleType]) -> list[str]:
    missing = []
    for module_name, symbols in REQUIRED_NPI_SYMBOLS.items():
        module = modules.get(module_name)
        if module is None:
            missing.extend(f"{module_name}.{symbol}" for symbol in symbols)
            continue
        missing.extend(
            f"{module_name}.{symbol}"
            for symbol in symbols
            if not hasattr(module, symbol)
        )
    return missing


def unexpected_npi_module_origins(
    npi_root: Path,
    modules: dict[str, ModuleType],
) -> dict[str, str]:
    root = npi_root.resolve()
    unexpected = {}
    for module_name, module in modules.items():
        origin = getattr(module, "__file__", None)
        if not origin:
            unexpected[module_name] = "<unknown>"
            continue
        resolved = Path(origin).resolve()
        if resolved != root and root not in resolved.parents:
            unexpected[module_name] = str(resolved)
    return unexpected


def is_verified_release(release: str) -> bool:
    return release in VERIFIED_VERDI_RELEASES
