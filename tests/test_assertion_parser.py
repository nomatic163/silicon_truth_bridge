from stb.assertions import parse_assertion_source


def test_named_property_fixed_delay_is_structured() -> None:
    result = parse_assertion_source(
        "a_req: assert property (p_req);",
        property_source="""
        property p_req;
          @(posedge clk) disable iff (!rst_n)
            req |-> ##2 ack;
        endproperty
        """,
    )

    assert result["fidelity"]["syntax"] == "exact"
    assert result["fidelity"]["temporal"] == "exact"
    assert result["assertion"]["property_form"] == "named"
    assert result["clock"]["edge"] == "posedge"
    assert result["implication"]["operator"] == "|->"
    assert result["antecedent"]["steps"][0]["relative_window"] == {
        "min_cycles": 0,
        "max_cycles": 0,
    }
    assert result["consequent"]["steps"][0]["relative_window"] == {
        "min_cycles": 2,
        "max_cycles": 2,
    }


def test_inline_range_delay_remains_symbolic() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(negedge clk) disable iff (!rst_n)
            $rose(req) |=> ##[1:3] (ack && ready)
        );
        """
    )

    assert result["fidelity"]["temporal"] == "exact"
    assert result["implication"]["consequent_start"] == (
        "next_cycle_after_antecedent_match"
    )
    assert result["consequent"]["steps"] == [
        {
            "relative_window": {"min_cycles": 1, "max_cycles": 3},
            "expression": {
                "raw": "ack && ready",
                "representation": "raw",
                "dependency_status": "unresolved",
                "identifier_tokens": ["ack", "ready"],
                "macro_tokens": [],
                "sampled_functions": [],
                "unsupported_system_functions": [],
            },
        }
    ]
    sampled = result["antecedent"]["steps"][0]["expression"]["sampled_functions"]
    assert sampled[0]["name"] == "$rose"


def test_macro_leaf_is_opaque_without_hiding_timing() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(posedge clk) `REQ_VALID |-> ##2 ack
        );
        """
    )

    assert result["fidelity"]["temporal"] == "exact"
    assert result["fidelity"]["dependencies"] == "opaque"
    expression = result["antecedent"]["steps"][0]["expression"]
    assert expression["macro_tokens"] == ["`REQ_VALID"]
    assert expression["identifier_tokens"] == []


def test_advanced_sequence_disables_temporal_lowering() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(posedge clk) req |-> first_match(##[1:3] ack)
        );
        """
    )

    assert result["fidelity"]["syntax"] == "exact"
    assert result["fidelity"]["temporal"] == "unsupported"
    assert result["consequent"]["steps"] == []
    assert result["unsupported_constructs"] == ["first_match"]


def test_unbounded_delay_is_unsupported() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(posedge clk) req |-> ##[1:$] ack
        );
        """
    )

    assert result["fidelity"]["temporal"] == "unsupported"
    assert result["unsupported_constructs"] == ["invalid_or_unbounded_delay"]


def test_unreviewed_system_function_keeps_dependency_opaque() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(posedge clk) $changed(req) |-> ##1 ack
        );
        """
    )

    expression = result["antecedent"]["steps"][0]["expression"]
    assert expression["unsupported_system_functions"] == ["$changed"]
    assert expression["dependency_status"] == "opaque"
    assert result["fidelity"]["dependencies"] == "opaque"


def test_outer_parenthesized_property_is_supported() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(posedge clk) (req |-> ##1 ack)
        );
        """
    )

    assert result["fidelity"]["temporal"] == "exact"
    assert result["implication"]["operator"] == "|->"


def test_sequence_boolean_keyword_is_unsupported() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(posedge clk) req |-> (ack or ready)
        );
        """
    )

    assert result["fidelity"]["temporal"] == "unsupported"
    assert result["unsupported_constructs"] == ["or"]


def test_empty_sequence_term_is_unsupported() -> None:
    result = parse_assertion_source(
        """
        a_req: assert property (
          @(posedge clk) req |-> ##1 ##2 ack
        );
        """
    )

    assert result["fidelity"]["temporal"] == "unsupported"
    assert result["unsupported_constructs"] == ["empty_sequence_term"]
