import json
from pathlib import Path

import clawjournal


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_claude_marketplace_points_to_plugin_wrapper():
    marketplace = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert marketplace["name"] == "clawjournal"
    assert marketplace["version"] == clawjournal.__version__
    assert len(marketplace["plugins"]) == 1
    plugin = marketplace["plugins"][0]
    assert plugin["name"] == "clawjournal"
    assert plugin["version"] == clawjournal.__version__
    assert plugin["source"] == "../plugins/clawjournal"
    assert plugin["category"] == "productivity"
    assert plugin["author"]["name"] == "rayward-external"
    assert plugin.get("description"), "plugin description must be present but can evolve freely"


def test_plugin_wrapper_uses_root_skills_via_symlink():
    plugin_root = REPO_ROOT / "plugins" / "clawjournal"
    plugin_manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text())
    skills_link = plugin_root / "skills"

    assert plugin_manifest["name"] == "clawjournal"
    assert plugin_manifest["version"] == clawjournal.__version__
    assert skills_link.is_symlink()
    assert skills_link.resolve() == (REPO_ROOT / "skills").resolve()
