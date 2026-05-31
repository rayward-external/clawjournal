# Releasing ClawJournal

Releases publish to [PyPI](https://pypi.org/project/clawjournal/) automatically
via `.github/workflows/publish.yml` when a `vX.Y.Z` tag is pushed. Publishing
uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC),
so there is **no API token to store or rotate**.

## One-time setup (do this once, before the first tagged release)

1. On the PyPI project's
   [publishing settings](https://pypi.org/manage/project/clawjournal/settings/publishing/),
   add a **GitHub** trusted publisher:
   - **Owner:** `rayward-external`
   - **Repository:** `clawjournal`
   - **Workflow name:** `publish.yml`
   - **Environment:** `pypi`
2. *(Optional)* In **repo Settings → Environments**, create an environment named
   `pypi`. Add required reviewers there if you want a manual approval gate before
   each publish.

Until step 1 is done, the `publish` job will fail with an OIDC/authentication
error — that is expected, not a workflow bug.

## Cutting a release

1. **Bump the version** in lockstep across all four files
   (`tests/test_repo_layout.py` enforces that they agree):
   - `clawjournal/__init__.py` — `__version__`
   - `pyproject.toml` — `[project].version`
   - `.claude-plugin/marketplace.json` — top-level `version` **and** `plugins[0].version`
   - `plugins/clawjournal/.claude-plugin/plugin.json` — `version`
2. Open a `release: X.Y.Z` PR, let CI pass, and merge.
3. Tag the merged commit and push the tag:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "ClawJournal X.Y.Z"
   git push origin vX.Y.Z
   ```
4. The workflow builds the browser workbench, builds the sdist + wheel, verifies
   the wheel ships `web/frontend/dist/index.html`, checks the tag matches the
   package version, and publishes to PyPI.
5. *(Optional)* Create a GitHub Release for the tag with notes:
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z — <summary>" --notes "..."
   ```

## Notes

- The tag's commit must contain `publish.yml`, so always tag from `main` after
  the version-bump PR is merged.
- `skip-existing: true` means re-running a tag already on PyPI is a no-op; a
  genuine new release still requires a version bump (a mismatched tag is rejected
  by the version-check step).
- No need to build or upload locally anymore — the manual `python -m build` +
  `twine upload` flow is fully replaced by the tag trigger.
