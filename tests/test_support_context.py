from __future__ import annotations

import json

from clawjournal import support_context


def _environment(monkeypatch) -> support_context.SupportEnvironment:
    monkeypatch.setattr(support_context.platform, "system", lambda: "Linux")
    monkeypatch.setattr(support_context.platform, "release", lambda: "6.8.0-generic")
    monkeypatch.setattr(support_context.platform, "machine", lambda: "x86_64")
    return support_context.capture_support_environment(
        revision="a" * 40,
        expected_user_version=12,
        sqlite_version="3.49.1",
    )


def test_collect_support_context_has_an_exact_allowlisted_shape(monkeypatch):
    environment = _environment(monkeypatch)

    report = support_context.collect_support_context(
        environment,
        {
            "status": "unavailable",
            "code": "storage_migration_required",
            "filesystem_type": "nfs4",
            "storage_risk": "network",
            "storage_migration_required": True,
        },
    )

    assert report == {
        "support_context_schema_version": 1,
        "kind": "workbench",
        "package": {"version": "0.2.0", "revision": "a" * 12},
        "runtime": {
            "python_version": (
                f"{support_context.sys.version_info.major}."
                f"{support_context.sys.version_info.minor}."
                f"{support_context.sys.version_info.micro}"
            ),
            "sqlite_version": "3.49.1",
            "os_family": "Linux",
            "os_release": "6.8.0",
            "architecture": "x86_64",
        },
        "schema": {"expected_user_version": 12},
        "storage": {
            "filesystem_type": "nfs4",
            "storage_risk": "network",
            "storage_migration_required": True,
        },
        "index": {
            "status": "unavailable",
            "condition": "storage_migration_required",
        },
        "collection": {"status": "complete", "unavailable_sections": []},
    }


def test_malicious_startup_and_health_fields_cannot_escape_allowlist(
    monkeypatch,
):
    canary = "alice-private /home/alice/session-123?token=secret"
    monkeypatch.setattr(support_context, "__version__", canary)
    monkeypatch.setattr(support_context.platform, "system", lambda: canary)
    monkeypatch.setattr(support_context.platform, "release", lambda: canary)
    monkeypatch.setattr(support_context.platform, "machine", lambda: canary)
    environment = support_context.capture_support_environment(
        revision=canary,
        expected_user_version=canary,
        sqlite_version=canary,
    )

    report = support_context.collect_support_context(
        environment,
        {
            "status": canary,
            "code": canary,
            "filesystem_type": "fuse.alice_private_mount",
            "storage_risk": canary,
            "message": canary,
            "detail": canary,
            "database_path": canary,
            "backup_path": canary,
            "session_id": canary,
            "project": canary,
        },
    )
    serialized = json.dumps(report)

    assert canary not in serialized
    assert "/home/" not in serialized
    assert "session_id" not in serialized
    assert "database_path" not in serialized
    assert report["package"] == {"version": "unknown", "revision": None}
    assert report["storage"] == {
        "filesystem_type": "unknown",
        "storage_risk": "unknown",
        "storage_migration_required": False,
    }
    assert report["index"] == {"status": "unknown", "condition": None}
    assert report["collection"]["status"] == "partial"
    assert set(report["collection"]["unavailable_sections"]) == {
        "package_version",
        "package_revision",
        "runtime_sqlite",
        "runtime_os",
        "runtime_architecture",
        "expected_schema",
        "cached_index_health",
    }


def test_process_local_environment_is_revalidated_before_serialization():
    canary = "alice-private /home/alice/session-123?token=secret"
    environment = support_context.SupportEnvironment(
        package_version=canary,
        package_revision=canary,
        python_version=canary,
        sqlite_version=canary,
        os_family=canary,
        os_release=canary,
        architecture=canary,
        expected_user_version=canary,  # type: ignore[arg-type]
        unavailable_sections=(canary,),
    )

    report = support_context.collect_support_context(
        environment,
        {
            "status": "ready",
            "filesystem_type": "ext4",
            "storage_risk": "local",
            "storage_migration_required": False,
        },
    )

    assert canary not in json.dumps(report)
    assert report["package"] == {"version": "unknown", "revision": None}
    assert report["runtime"] == {
        "python_version": "unknown",
        "sqlite_version": "unknown",
        "os_family": "unknown",
        "os_release": "unknown",
        "architecture": "unknown",
    }
    assert report["schema"] == {"expected_user_version": None}
    assert report["collection"]["status"] == "partial"


def test_environment_collection_exceptions_return_fixed_partial_values(
    monkeypatch,
):
    canary = "failure at /home/alice/private"

    def fail():
        raise RuntimeError(canary)

    monkeypatch.setattr(support_context.platform, "system", fail)
    monkeypatch.setattr(support_context.platform, "machine", fail)

    environment = support_context.capture_support_environment(
        revision=None,
        expected_user_version=12,
        sqlite_version="3.49.1",
    )
    report = support_context.collect_support_context(
        environment,
        {
            "status": "ready",
            "filesystem_type": "ext4",
            "storage_risk": "local",
            "storage_migration_required": False,
        },
    )

    assert report["runtime"]["os_family"] == "unknown"
    assert report["runtime"]["os_release"] == "unknown"
    assert report["runtime"]["architecture"] == "unknown"
    assert report["collection"] == {
        "status": "partial",
        "unavailable_sections": [
            "package_revision",
            "runtime_os",
            "runtime_architecture",
        ],
    }
    assert canary not in json.dumps(report)


def test_unavailable_support_context_is_fixed_and_fully_partial():
    report = support_context.unavailable_support_context()

    assert report["collection"] == {
        "status": "partial",
        "unavailable_sections": [
            "package_version",
            "package_revision",
            "runtime_python",
            "runtime_sqlite",
            "runtime_os",
            "runtime_architecture",
            "expected_schema",
            "cached_index_health",
        ],
    }
    assert report["index"] == {"status": "unknown", "condition": None}
