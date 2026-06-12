"""Render + sanitization tests."""

from __future__ import annotations

from clawjournal.events.doctor import render
from clawjournal.events.doctor.probes import (
    ClientObservation,
    DoctorReport,
    INSTALL_HEALTHY,
    TruffleHogStatus,
    VERDICT_COMPATIBLE,
    VERDICT_PARTIAL,
)


def _base_report(**overrides) -> DoctorReport:
    defaults = dict(
        install_state=INSTALL_HEALTHY,
        install_hint="ok",
        clawjournal_version="0.0.2",
        bundle_schema_version="1.0",
        recorder_schema_version="1.0",
        security_schema_version=2,
        config_dir="/Users/synthetic-user/.clawjournal",
        index_db_path="/Users/synthetic-user/.clawjournal/index.db",
        events_count=10,
        sessions_count=2,
        trufflehog=TruffleHogStatus(state="present", version="3.94.3"),
        clients=[],
        fs_clients=[],
        cost=None,
        incidents=None,
        warnings=[],
    )
    defaults.update(overrides)
    return DoctorReport(**defaults)


def test_sanitize_strips_ansi_escapes():
    out = render.sanitize_for_human("\x1b[31mred\x1b[0m")
    assert "\x1b" not in out
    assert "31m" in out  # the escape body is printable; the ESC byte is gone


def test_sanitize_strips_control_chars():
    out = render.sanitize_for_human("hello\nworld\x07bell")
    assert "\n" not in out
    assert "\x07" not in out


def test_sanitize_caps_length():
    out = render.sanitize_for_human("a" * 500)
    assert len(out) <= 201  # 200 + the ellipsis char


def test_render_human_includes_versions():
    text = render.render_human(_base_report())
    assert "bundle_schema_version" in text
    assert "recorder_schema_version" in text
    assert "security_schema_version" in text


def test_render_human_flags_off_pin_managed_trufflehog():
    report = _base_report(
        trufflehog=TruffleHogStatus(
            state="present", version="3.94.3", off_pin_expected="3.95.5"
        )
    )
    text = render.render_human(report)
    assert "behind pin v3.95.5" in text
    assert "clawjournal trufflehog install" in text


def test_render_human_no_off_pin_noise_when_pin_matched():
    text = render.render_human(_base_report())
    assert "behind pin" not in text


def test_render_human_sanitizes_client_version():
    obs = ClientObservation(
        client="claude",
        client_version="1.42\n\x07evil",
        sessions_count=1,
        event_types_observed=["user_message"],
        unknown_event_types=[],
        unsupported_event_types=[],
        schema_unknown_rows=0,
        matrix_supported_count=11,
        verdict=VERDICT_COMPATIBLE,
    )
    text = render.render_human(_base_report(clients=[obs]))
    assert "\x07" not in text
    assert "\n\x07" not in text


def test_render_json_carries_schema_version_and_request_id():
    import json

    text = render.render_json(_base_report(), request_id="req-xyz")
    payload = json.loads(text)
    assert payload["events_doctor_schema_version"] == "1.0"
    assert payload["_meta"]["request_id"] == "req-xyz"


def test_render_json_omits_meta_without_request_id():
    import json

    payload = json.loads(render.render_json(_base_report()))
    assert "_meta" not in payload


def test_partial_suggestion_does_not_reference_nonexistent_inspect_flag():
    """Round 7: the schema_unknown suggestion must not point users at
    `events inspect --type schema_unknown` — that flag was in the plan
    sketch but never landed in the parser. The hint must use a CLI
    shape that actually works today (`events inspect <event_id>`)."""

    obs = ClientObservation(
        client="claude",
        client_version="1.45.0",
        sessions_count=1,
        event_types_observed=["schema_unknown"],
        unknown_event_types=[],
        unsupported_event_types=[],
        schema_unknown_rows=2,
        matrix_supported_count=11,
        verdict=VERDICT_PARTIAL,
    )
    text = render.render_human(_base_report(clients=[obs]))
    assert "Suggested next steps" in text
    assert "--type schema_unknown" not in text
    assert "events inspect <event_id>" in text
