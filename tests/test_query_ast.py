import pytest

from stb.backends.fake import FakeBackend
from stb.errors import StbError
from stb.query import evaluate_where
from stb.schemas import validate_public_request


def test_fake_query_where_ast() -> None:
    backend = FakeBackend("ctx")
    result = backend.object_query(
        {
            "scope": "top.u_core",
            "where": {
                "op": "all",
                "args": [
                    {"op": "glob", "property": "name", "value": "d*"},
                    {"op": "ge", "property": "width", "value": 32},
                ],
            },
        }
    )
    assert [row["ref"]["full_name"] for row in result["objects"]] == [
        "top.u_core.data"
    ]


def test_query_ast_rejects_unknown_property_and_bad_regex() -> None:
    with pytest.raises(StbError, match="not queryable"):
        FakeBackend("ctx").object_query(
            {"where": {"op": "eq", "property": "secret", "value": 1}}
        )
    with pytest.raises(StbError, match="invalid where regex"):
        evaluate_where(
            {"op": "regex", "property": "name", "value": "["},
            lambda _: "value",
        )


def test_public_schema_rejects_invalid_where_ast() -> None:
    with pytest.raises(StbError) as error:
        validate_public_request(
            "object_query",
            {
                "context_id": "ctx",
                "request": {
                    "where": {
                        "op": "eq",
                        "property": "name",
                        "value": "top",
                        "extra": True,
                    }
                },
            },
        )
    assert error.value.code == "invalid_request"


def test_public_schema_rejects_unknown_traverse_filter_shape() -> None:
    with pytest.raises(StbError) as error:
        validate_public_request(
            "object_traverse",
            {
                "context_id": "ctx",
                "request": {
                    "roots": [
                        {
                            "model": "netlist",
                            "context_id": "ctx",
                            "worker_generation": 1,
                            "npi_type": "INST",
                            "full_name": "top",
                        }
                    ],
                    "relation": "children",
                    "filters": {
                        "name": {"op": "eq", "property": "name", "value": "u"}
                    },
                },
            },
        )
    assert error.value.code == "invalid_request"

    with pytest.raises(StbError) as error:
        validate_public_request(
            "object_query",
            {
                "context_id": "ctx",
                "request": {
                    "where": {
                        "op": "all",
                        "args": [],
                    }
                },
            },
        )
    assert error.value.code == "invalid_request"


def test_public_schema_rejects_invalid_expression_and_mapping_profile() -> None:
    with pytest.raises(StbError) as error:
        validate_public_request(
            "wave_compute",
            {
                "context_id": "ctx",
                "request": {
                    "operation": "evaluate_window",
                    "wave_id": "run",
                    "start": "0ns",
                    "end": "1ns",
                    "expression": {"op": "python.eval", "args": []},
                },
            },
        )
    assert error.value.code == "invalid_request"

    with pytest.raises(StbError) as error:
        validate_public_request(
            "mapping",
            {
                "context_id": "ctx",
                "request": {
                    "action": "validate",
                    "profile": {
                        "rules": [
                            {"kind": "prefix_replace", "source_prefix": "a."}
                        ]
                    },
                },
            },
        )
    assert error.value.code == "invalid_request"


def test_public_schema_accepts_versioned_expression_envelope() -> None:
    validated = validate_public_request(
        "wave_compute",
        {
            "context_id": "ctx",
            "request": {
                "operation": "evaluate_window",
                "wave_id": "run",
                "start": "0ns",
                "end": "20ns",
                "max_points": 20,
                "expression": {
                    "expr_version": "stb.expr.v1",
                    "root": {
                        "op": "logic.eq",
                        "args": [
                            {"signal": "top.req"},
                            {"literal": "1'b1"},
                        ],
                    },
                },
            },
        },
    )
    result = FakeBackend(
        "ctx", wave_specs=[{"wave_id": "run", "path": "fake.fsdb"}]
    ).wave_compute(validated["request"])
    assert result["operation"] == "evaluate_window"
    assert {row["value"]["value"] for row in result["rows"]} == {"0", "1"}


def test_public_schema_rejects_missing_wave_compute_operation_inputs() -> None:
    with pytest.raises(StbError) as error:
        validate_public_request(
            "wave_compute",
            {
                "context_id": "ctx",
                "request": {
                    "operation": "statistics",
                    "wave_id": "run",
                    "signals": ["top.req"],
                },
            },
        )
    assert error.value.code == "invalid_request"
