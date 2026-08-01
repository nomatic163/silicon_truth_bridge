from stb.backends.fake import FakeBackend
from stb.service import StbService


def test_fake_assertion_structure_is_anchored_and_resolved() -> None:
    service = StbService(FakeBackend("rtl"), "rtl")
    resolved = service.object_resolve(
        {
            "model": "language",
            "name": "top.a_req_to_data",
        }
    )
    result = service._call(
        "assertion_structure",
        lambda: service.backend.assertion_structure(
            {"reference": resolved["data"]["ref"]}
        ),
    )

    assert result["status"] == "complete"
    data = result["data"]
    assert data["anchor"]["semantic_class"] == "concurrent_assertion"
    assert data["source_evidence"]["change_status"] == "unchanged"
    assert data["structure"]["fidelity"] == {
        "syntax": "exact",
        "temporal": "exact",
        "dependencies": "exact",
    }
    consequent = data["structure"]["consequent"]["steps"][0]
    assert consequent["relative_window"] == {
        "min_cycles": 1,
        "max_cycles": 3,
    }
    resolved_tokens = consequent["expression"]["resolved_identifiers"]
    assert resolved_tokens[0]["object"]["ref"]["full_name"] == "top.u_core.data"


def test_fake_assertion_query_returns_language_object_ref() -> None:
    service = StbService(FakeBackend("rtl"), "rtl")
    result = service.object_query(
        {
            "model": "language",
            "scope": "top",
            "npi_types": ["npiAssert"],
            "limit": 10,
        }
    )

    assert result["status"] == "complete"
    ref = result["data"]["objects"][0]["ref"]
    assert ref["model"] == "language"
    assert ref["npi_type"] == "npiAssert"
