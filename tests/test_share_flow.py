"""Tests for the shared share-flow service (classifiers, coverage, destination)."""
from clawjournal import share_flow as sf


def test_bucket_for_classification():
    assert sf.bucket_for("email_address") == "emails"
    assert sf.bucket_for("private_url") == "urls"
    assert sf.bucket_for("file_path") == "paths"
    assert sf.bucket_for("username") == "paths"
    assert sf.bucket_for("timestamp") == "timestamps"
    assert sf.bucket_for("high_entropy_secret") == "tokens"
    assert sf.bucket_for("trufflehog.AWS") == "tokens"
    assert sf.bucket_for("something_else") == "other"


def test_redaction_buckets_counts_trufflehog():
    log = [
        {"type": "email"}, {"type": "email"},
        {"type": "file_path"},
        {"type": "trufflehog.AWS"}, {"type": "trufflehog.GCP"},
        {"type": "api_token"},
        {"type": "weird"},
    ]
    buckets, th = sf.redaction_buckets(log)
    assert buckets["emails"] == 2
    assert buckets["paths"] == 1
    assert buckets["tokens"] == 3  # 2 trufflehog + 1 token
    assert buckets["other"] == 1
    assert th == 2


def test_category_breakdown_labels_and_trufflehog_callout():
    rec = {
        "buckets": {"tokens": 5, "emails": 1, "paths": 0, "urls": 0,
                    "timestamps": 0, "other": 0},
        "th_hits": 4,
        "ai_findings": [{"entity_type": "person_name"}, {"entity_type": "person_name"}],
    }
    out = sf.category_breakdown(rec)
    assert "Secrets & credentials (incl. 4 via TruffleHog): 5" in out
    assert "Email addresses: 1" in out
    assert "AI-flagged person name: 2" in out


def test_trace_status():
    assert sf.trace_status({"ai_coverage": "disabled"}) == "review"
    assert sf.trace_status({"ai_coverage": "rules_only"}) == "review"
    assert sf.trace_status({"ai_coverage": "full", "ai_findings": []}) == "clear"
    assert sf.trace_status({"ai_coverage": "full",
                            "ai_findings": [{"confidence": 0.5}]}) == "review"
    assert sf.trace_status({"ai_coverage": "full",
                            "ai_findings": [{"confidence": 0.95}]}) == "clear"


def test_effective_ai_pii_keeps_preview_consistent():
    # not requested -> off, uniform
    assert sf.effective_ai_pii([{"ai_coverage": "disabled"}], False) == (False, True)
    # all full -> on, uniform
    assert sf.effective_ai_pii([{"ai_coverage": "full"}, {"ai_coverage": "full"}], True) == (True, True)
    # any rules_only -> degrade off, not uniform (caller must reconcile)
    assert sf.effective_ai_pii([{"ai_coverage": "full"}, {"ai_coverage": "rules_only"}], True) == (False, False)


def test_package_checks_release_gate_before_creating_share(monkeypatch):
    blockers = [{"session_id": "held", "hold_state": "pending_review"}]
    monkeypatch.setattr(sf, "gate_blockers", lambda _conn, _ids: blockers)
    monkeypatch.setattr(
        sf,
        "create_share",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create")),
    )

    result = sf.package(
        None,
        ["held"],
        {"source_filter": ["codex"]},
        ai_pii=True,
        ai_backend="codex",
    )

    assert result["ok"] is False
    assert result["blockers"] == blockers


def test_verify_coverage_rejects_wrong_backend():
    manifest = {
        "redaction_summary": {
            "pii_review": {
                "ai_enabled": True,
                "backend": "claude",
                "coverage": {"full": 1, "rules_only": 0},
            }
        }
    }

    ok, reason = sf.verify_coverage(
        manifest,
        package_ai=True,
        expected_backend="codex",
    )

    assert ok is False
    assert "different AI-PII backend" in reason


def test_hosted_destination_unreachable(monkeypatch):
    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(sf, "_fetch_hosted_share_capabilities", boom)
    info = sf.hosted_destination()
    assert info["reachable"] is False
    assert info["can_submit"] is False
    assert info["can_download"] is True


def test_hosted_destination_closed(monkeypatch):
    monkeypatch.setattr(sf, "_fetch_hosted_share_capabilities",
                        lambda: {"submissions_open": False, "contact_email": "x@y.edu"})
    info = sf.hosted_destination()
    assert info["reachable"] is True
    assert info["can_submit"] is False
    assert info["support_contact"] == "x@y.edu"


def test_hosted_destination_open(monkeypatch):
    monkeypatch.setattr(sf, "_fetch_hosted_share_capabilities",
                        lambda: {"submissions_open": True, "maximum_bundle_size": 100})
    info = sf.hosted_destination()
    assert info["can_submit"] is True
    assert info["maximum_bundle_size"] == 100
