from clawjournal.cli import _mask_config_for_display


def test_config_display_masks_secret_keys_recursively():
    displayed = _mask_config_for_display(
        {
            "verified_email": "person@example.edu",
            "verified_email_token": "abcdefghijklmnop",
            "recurring_enrollment_grant": "grant-abcdefghijklmnop",
            "nested": {
                "api_key": "sk-super-secret-value",
                "safe": "visible",
            },
            "redact_strings": ["private.example", "short"],
        }
    )

    assert displayed["verified_email"] == "person@example.edu"
    assert displayed["verified_email_token"] == "abcd...mnop"
    assert displayed["recurring_enrollment_grant"] == "gran...mnop"
    assert displayed["nested"] == {
        "api_key": "sk-s...alue",
        "safe": "visible",
    }
    assert displayed["redact_strings"] == ["priv...mple", "***"]
