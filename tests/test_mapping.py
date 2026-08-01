from stb.backends.fake import FakeBackend


def test_fake_mapping_reports_pipeline_and_generation_cache() -> None:
    backend = FakeBackend(
        "ctx",
        wave_specs=[{"wave_id": "run", "path": "fake.fsdb"}],
    )
    args = {
        "action": "resolve",
        "wave_id": "run",
        "design_full_name": "top.u_core.req",
        "waveform_full_name": "top.req",
    }
    first = backend.mapping(args)
    second = backend.mapping(args)
    assert first["cache"] == "miss"
    assert second["cache"] == "hit"
    assert first["pipeline"][0]["status"] == "matched"
    assert first["actual_name_evidence"]["status"] == "unavailable"
    assert first["bit_mapping"]["kind"] == "identity"
    assert first["wave_generation"] == 1


def test_fake_mapping_validate_does_not_require_wave() -> None:
    result = FakeBackend("ctx").mapping(
        {
            "action": "validate",
            "profile": {
                "rules": [
                    {
                        "kind": "prefix_replace",
                        "source_prefix": "a.",
                        "target_prefix": "b.",
                    }
                ]
            },
        }
    )
    assert result == {"valid": True, "rule_count": 1}
