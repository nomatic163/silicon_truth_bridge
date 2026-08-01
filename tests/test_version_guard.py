import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from stb.backends.verdi import VerdiBackend
from stb.config import Settings
from stb.errors import StbError
from stb.service import StbService
from stb.verdi_compat import (
    REQUIRED_NPI_SYMBOLS,
    VERIFIED_VERDI_RELEASES,
    detect_verdi_release,
    missing_npi_symbols,
    unexpected_npi_module_origins,
)


def test_verdi_release_path_is_rejected_before_npi_import(tmp_path) -> None:
    with pytest.raises(StbError) as error:
        VerdiBackend(
            context_id="bad",
            verdi_home=str(tmp_path / "W-2099.01"),
            allow_unverified_verdi=False,
        )
    assert error.value.code == "unsupported_api_version"
    assert error.value.details["detected"] == "W-2099.01"


def test_default_verdi_path_names_verified_release() -> None:
    assert "V-2023.12-SP1" in VERIFIED_VERDI_RELEASES


def test_release_detection_follows_versioned_symlink(tmp_path) -> None:
    release_home = tmp_path / "V-2023.12-SP1"
    release_home.mkdir()
    current = tmp_path / "current"
    current.symlink_to(release_home, target_is_directory=True)

    assert detect_verdi_release(current) == "V-2023.12-SP1"
    assert detect_verdi_release(current, override="V-2024.09") == "V-2024.09"


def test_required_npi_symbol_probe_reports_missing_symbol() -> None:
    modules = {
        module_name: SimpleNamespace(
            **{symbol: object() for symbol in symbols}
        )
        for module_name, symbols in REQUIRED_NPI_SYMBOLS.items()
    }
    assert missing_npi_symbols(modules) == []

    delattr(modules["waveform"], "sig_value_at")
    assert missing_npi_symbols(modules) == ["waveform.sig_value_at"]


def test_npi_module_origin_probe_rejects_another_release(tmp_path) -> None:
    selected = tmp_path / "selected" / "share/NPI/python"
    selected.mkdir(parents=True)
    modules = {
        "npisys": SimpleNamespace(__file__=selected / "pynpi/npisys.py"),
        "waveform": SimpleNamespace(
            __file__=tmp_path / "other/share/NPI/python/pynpi/waveform.py"
        ),
    }

    assert unexpected_npi_module_origins(selected, modules) == {
        "waveform": str(Path(modules["waveform"].__file__).resolve())
    }


def test_settings_fall_back_to_standard_verdi_home(monkeypatch, tmp_path) -> None:
    verdi_home = tmp_path / "V-2023.12-SP1"
    worker_python = tmp_path / "python"
    monkeypatch.delenv("STB_VERDI_HOME", raising=False)
    monkeypatch.setenv("VERDI_HOME", str(verdi_home))
    monkeypatch.setenv("STB_WORKER_PYTHON", str(worker_python))

    settings = Settings()

    assert settings.verdi_home == verdi_home
    assert settings.worker_python == worker_python


def test_unverified_release_reports_actual_version(monkeypatch, tmp_path) -> None:
    verdi_home = tmp_path / "W-2099.01"
    npi_root = verdi_home / "share/NPI/python"
    npi_root.mkdir(parents=True)
    modules = {
        module_name: SimpleNamespace(
            __file__=npi_root / "pynpi" / f"{module_name}.py",
            **{symbol: object() for symbol in symbols}
        )
        for module_name, symbols in REQUIRED_NPI_SYMBOLS.items()
    }
    modules["npisys"].init = lambda argv: True
    modules["npisys"].end = lambda: True
    modules["npisys"].load_design = lambda argv: True
    package = ModuleType("pynpi")
    for module_name, module in modules.items():
        setattr(package, module_name, module)
    monkeypatch.setitem(sys.modules, "pynpi", package)

    monkeypatch.setenv("VERDI_HOME", str(tmp_path / "stale-verdi"))
    backend = VerdiBackend(
        context_id="future",
        verdi_home=str(verdi_home),
        allow_unverified_verdi=True,
    )
    try:
        response = StbService(backend, "future").catalog(
            "backend_capabilities"
        )
        assert response["receipt"]["verdi_version"] == "W-2099.01"
        assert response["receipt"]["verdi_compatibility"] == "unverified"
        assert response["data"]["verdi_version"] == "W-2099.01"
        assert response["data"]["verified_verdi"] is False
        assert os.environ["VERDI_HOME"] == str(verdi_home)
    finally:
        backend.close()
