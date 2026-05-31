"""Cross-language guard tests.

The React frontend has no JS test runner (only the wheel smoke check), and a few
backend↔frontend behaviors are coupled by fragile string conventions. These read
the .tsx source and pin those contracts from Python so a rename can't silently
break them.
"""

import re
from pathlib import Path

from clawjournal.benchmark import schema as bm
from clawjournal.benchmark.render import EXPORT_KINDS

_REPO = Path(__file__).resolve().parents[2]
BENCHMARK_TSX = _REPO / "clawjournal" / "web" / "frontend" / "src" / "views" / "Benchmark.tsx"


def _tsx() -> str:
    return BENCHMARK_TSX.read_text(encoding="utf-8")


def test_export_kinds_match_backend():
    """The UI Export menu must offer exactly the kinds render.render accepts (S9)."""
    m = re.search(r"const EXPORT_KINDS.*?=\s*\[(.*?)\];", _tsx(), re.S)
    assert m, "EXPORT_KINDS array not found in Benchmark.tsx"
    fe_kinds = set(re.findall(r"kind:\s*'([a-z_]+)'", m.group(1)))
    assert fe_kinds == set(EXPORT_KINDS), f"frontend {fe_kinds} != backend {set(EXPORT_KINDS)}"


def test_agent_prompt_template_has_no_grader_fields():
    """The 'Copy prompt' template must reference only agent-packet fields (S10)."""
    m = re.search(r"const agentPrompt = `(.*?)`;", _tsx(), re.S)
    assert m, "agentPrompt template not found in Benchmark.tsx"
    template = m.group(1)
    for field in bm.GRADER_ONLY_FIELDS:
        assert field not in template, f"agent prompt template references grader field {field!r}"


def test_progress_prose_maps_to_stage_percent():
    """The backend's progress prose must contain the keywords the UI stagePercent
    matches, else a rename silently drops the progress bar to the fallback (S8)."""
    keywords = ["reading", "group", "writing", "finaliz", "done"]
    # frontend side: stagePercent recognises each keyword
    sp = _tsx().split("function stagePercent", 1)[1].split("\n}", 1)[0].lower()
    for kw in keywords:
        assert kw in sp, f"stagePercent lost keyword {kw!r}"
    # backend side: note() literals + the _map_progress label= literals cover them
    gen_src = (_REPO / "clawjournal" / "benchmark" / "generate.py").read_text(encoding="utf-8")
    strings = (re.findall(r'note\(\s*f?["\']([^"\']+)["\']', gen_src)
               + re.findall(r'label="([^"]+)"', gen_src))
    joined = " ".join(strings).lower()
    for kw in keywords:
        assert kw in joined, f"no backend progress string contains stage keyword {kw!r}"
