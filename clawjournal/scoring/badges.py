"""Compute trace card badges for the scientist workbench inbox."""

import re
from collections.abc import Iterator

from ..parsing.segmenter import strip_high_confidence_internal_wrappers
from ..redaction.secrets import scan_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "cat", "ls", "find", "head", "tail",
    "View", "Search", "ListFiles", "read_file", "search_files",
})

_SCIENTIFIC_LIBS = re.compile(
    r"\b(?:numpy|scipy|pandas|matplotlib|seaborn|plotly|biopython|BioPython"
    r"|rdkit|pytorch|torch|tensorflow|keras|jax|flax"
    r"|astropy|sympy|scikit-learn|sklearn|statsmodels"
    r"|openmm|mdtraj|pymatgen|ase|dask|xarray"
    r"|protein|genome|genomic|molecular|quantum|spectral"
    r"|phylogen|metabol|transcriptom|proteom)\b",
    re.IGNORECASE,
)

_SCIENTIFIC_EXTENSIONS = re.compile(
    r"\.(ipynb|csv|tsv|h5|hdf5|fasta|fastq|pdb|cif|mol2|sdf|npy|npz|parquet|feather)\b"
)

_SCIENTIFIC_TERMS = re.compile(
    r"\b(?:experiment|hypothesis|correlation|regression"
    r"|p-value|pvalue|chi-square|t-test|anova|standard.deviation"
    r"|confidence.interval|null.hypothesis|statistical|bayesian"
    r"|spectroscopy|chromatography|genome|proteomics|phylogenetic)\b",
    re.IGNORECASE,
)

_PRIVATE_URL = re.compile(
    r"https?://(?:"
    r"[a-zA-Z0-9._-]+\.(?:local|internal|corp|intranet|lan)"
    r"|localhost(?::\d+)?"
    r"|(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d+\.\d+"
    r")\b",
)

# Heuristic: two capitalized words that look like a person's name.
# Requires first word to be 2-6 chars (typical first name length) and the
# pattern must NOT start with a common verb/adjective/noun prefix that appears
# in code phrases like "Initial Translation", "Quality Control".
_CODE_PHRASE_PREFIX = re.compile(
    r"^(?:All|Any|Bad|Big|Fix|Get|Has|Max|Min|New|No|Not|Old|Our|Raw|Run|Set|The|Top|Try|Use"
    r"|Add|Auto|Base|Best|Bool|Code|Copy|Core|Data|Deep|Diff|Dump|Each|Edit|Emit|Even|Exec"
    r"|Exit|Fail|Fast|File|Fill|Find|Flag|Flow|Font|Fork|Form|Free|Full|Good|High|Http|Icon"
    r"|Init|Join|Just|Keep|Kill|Kind|Last|Late|Lazy|Left|Like|Line|Link|List|Load|Lock|Logo"
    r"|Long|Loop|Main|Make|Many|Mark|Mega|Mock|Mode|More|Most|Move|Much|Must|Name|Next|Null"
    r"|Only|Open|Over|Pack|Page|Pair|Pass|Path|Pick|Plan|Play|Post|Pure|Push|Read|Real|Redo"
    r"|Root|Safe|Same|Save|Scan|Send|Show|Side|Skip|Slow|Snap|Some|Sort|Spin|Step|Stop|Sure"
    r"|Sync|Take|Task|Test|Text|Then|Time|Tiny|Todo|Tool|Tree|Trim|True|Turn|Type|Undo|Unit"
    r"|Very|View|Wait|Walk|Want|Warn|With|Work|Wrap|Zero"
    r"|Batch|Build|Check|Clean|Clear|Close|Count|Debug|Draft|Empty|Error|Event|Every|Extra"
    r"|Fetch|Final|First|Force|Fresh|Given|Group|Guard|Guide|Heavy|Index|Inner|Input|Label"
    r"|Large|Later|Layer|Level|Light|Limit|Local|Lower|Match|Merge|Minor|Mixed|Model|Multi"
    r"|Never|Offer|Order|Other|Outer|Parse|Patch|Pause|Plain|Point|Print|Prior|Proxy|Query"
    r"|Queue|Quick|Quiet|Range|Rapid|React|Ready|Regex|Renew|Reply|Reset|Retry|Right|Route"
    r"|Scale|Score|Setup|Shall|Share|Sharp|Shift|Short|Since|Small|Smart|Sound|Space|Split"
    r"|Stack|Stage|Start|State|Still|Store|Strip|Style|Super|Sweet|Table|Theme|Third|Timer"
    r"|Title|Token|Total|Trace|Track|Trial|Upper|Usage|Using|Valid|Value|Watch|While|Whole"
    r"|Write|Wrong|Yield"
    r"|Accept|Active|Actual|Always|Amount|Assert|Assign|Backup|Before|Better|Bundle|Cancel"
    r"|Change|Choose|Client|Config|Create|Custom|Delete|Deploy|Design|Detail|Direct|Double"
    r"|Dragon|During|Enable|Engine|Entire|Expire|Export|Extend|Failed|Filter|Finish|Format"
    r"|Future|Global|Handle|Hidden|Higher|Import|Insert|Latest|Launch|Layout|Length|Linear"
    r"|Listen|Locale|Logger|Manage|Manual|Master|Mature|Medium|Memory|Method|Middle|Mobile"
    r"|Module|Native|Normal|Notice|Number|Object|Office|Online|Option|Origin|Output|Parent"
    r"|Prefer|Public|Random|Reader|Recent|Record|Reduce|Reform|Reject|Remote|Remove|Render"
    r"|Repeat|Report|Return|Review|Revert|Revoke|Rotate|Runner|Safety|Sample|Schema|Script"
    r"|Search|Secret|Secure|Select|Server|Signal|Simple|Single|Source|Stable|Static|Status"
    r"|Stored|Stream|Strict|String|Strong|Struct|Submit|System|Target|Toggle|Unique|Unsafe"
    r"|Update|Upload|Vector|Verify|Visual|Volume|Worker"
    r"|Cluster|Command|Comment|Compare|Compile|Complex|Connect|Content|Context|Control|Convert"
    r"|Current|Default|Display|Dynamic|Element|Encrypt|Execute|Express|Extract|Feature|General"
    r"|Generic|Handler|Inherit|Initial|Integer|Invalid|Loading|Machine|Mapping|Message|Migrate"
    r"|Minimum|Missing|Monitor|Natural|Network|Observe|Package|Palette|Pattern|Persist|Pointer"
    r"|Polling|Preview|Primary|Private|Process|Product|Profile|Program|Project|Promise|Protect"
    r"|Quality|Receive|Recycle|Regular|Release|Replace|Request|Require|Reserve|Resolve|Restart"
    r"|Restore|Sandbox|Service|Session|Setting|Shallow|Sorting|Storage|Summary|Support|Suspend"
    r"|Swagger|Testing|Thought|Timeout|Toolbar|Tracker|Trigger|Unknown|Utility|Virtual|Warning"
    r"|Wrapper"
    r"|Abstract|Analytic|Animated|Annotate|Assembly|Callback|Category|Compound|Consumer"
    r"|Critical|Database|Debugger|Decorate|Document|Download|Emission|Endpoint|Enqueued"
    r"|Fallback|Finished|Function|Generate|Glossary|Gradient|Hardware|Headline|Identity"
    r"|Implicit|Infinite|Instance|Internal|Interval|Iterable|Language|Metadata|Modifier"
    r"|Mutation|Nullable|Observer|Operator|Optional|Override|Parallel|Passphdr|Pipeline"
    r"|Platform|Portable|Possible|Prepared|Producer|Property|Protocol|Provider|Readable"
    r"|Recycler|Redirect|Refactor|Register|Renderer|Required|Resolver|Resource|Response"
    r"|Retrieve|Rollback|Rotation|Schedule|Selector|Semantic|Sequence|Skeleton|Snapshot"
    r"|Software|Specific|Standard|Stateful|Strategy|Template|Terminal|Throttle|Together"
    r"|Transfer|Validate|Variable|Viewport|Watchdog|Workflow"
    r")$",
    re.IGNORECASE,
)

_PROPER_NAME = re.compile(
    r"(?<![A-Za-z])"                     # not preceded by a letter
    r"[A-Z][a-z]{1,7}"                   # first name (2-8 chars)
    r"\s+[A-Z][a-z]{1,12}"              # last name (2-13 chars)
    r"(?![A-Za-z])",                      # not followed by a letter
)

# Common false-positive name patterns to skip
_NAME_ALLOWLIST = re.compile(
    r"\b(?:United States|New York|San Francisco|Los Angeles|Open Source"
    r"|Visual Studio|Stack Overflow|Pull Request|Merge Request"
    r"|Hello World|Status Code|Type Error|Value Error|Key Error"
    r"|Content Type|Access Control|No Content|Not Found"
    r"|Read Only|File System|Data Frame|Data Set"
    r"|Machine Learning|Deep Learning|Neural Network"
    r"|Test Case|Test Suite|Base Class|Default Value"
    r"|File Path|Error Message|Return Type|Input Type"
    r"|Output Format|Source Code|Ground Truth|Ground Foundry"
    r"|Command Line|Build Failed|Task Type|Review Status"
    r"|Start Time|End Time|Display Title|Session Detail"
    r"|Quality Score|Bundle Preview|Token Usage"
    r"|Claude Code|Trace Card|Badge Chip"
    r"|Stage Complete|Stage Failed|Translation Pipeline"
    r"|Output Directory|Input Directory|Current Branch"
    r"|Final Output|First Pass|Second Pass"
    r"|Next Steps|Last Updated|Not Applicable)\b",
)

_TASK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("debugging", re.compile(
        r"\b(?:fix|bug|error|broken|crash|issue|traceback|exception|failing|segfault"
        r"|debug|diagnose|troubleshoot|not working|doesn't work|won't work)\b",
        re.IGNORECASE,
    )),
    ("feature", re.compile(
        r"\b(?:add|implement|create|build|new feature|introduce|support for"
        r"|develop|make a|write a|generate|produce)\b",
        re.IGNORECASE,
    )),
    ("refactor", re.compile(
        r"\b(?:refactor|clean\s*up|reorganize|rename|move|restructure|simplify"
        r"|optimize|improve|update|change|modify|rewrite|rework|adjust|revise)\b",
        re.IGNORECASE,
    )),
    ("analysis", re.compile(
        r"\b(?:analyze|analyse|investigate|explore|understand|look at|inspect|audit"
        r"|figure out|find out|determine|assess|evaluate|compare|benchmark)\b",
        re.IGNORECASE,
    )),
    ("testing", re.compile(
        r"\b(?:write tests?|add tests?|test coverage|spec|unit test|integration test"
        r"|e2e test|end.to.end|test case|test suite|test plan)\b",
        re.IGNORECASE,
    )),
    ("documentation", re.compile(
        r"\b(?:document|readme|docstring|comment|changelog|update docs"
        r"|write docs|api docs|jsdoc|typedoc|sphinx|mkdocs"
        r"|translate|translation|convert|transform)\b",
        re.IGNORECASE,
    )),
    ("review", re.compile(
        r"\b(?:review|check|verify|validate|code review|pull request|pr review"
        r"|look over|go over|proofread)\b",
        re.IGNORECASE,
    )),
    ("configuration", re.compile(
        r"\b(?:set\s*up|setup|configure|install|deploy|provision|initialize|init"
        r"|bootstrap|scaffold|ci/?cd|docker|kubernetes)\b",
        re.IGNORECASE,
    )),
    ("migration", re.compile(
        r"\b(?:migrate|migration|upgrade|downgrade|port|transition"
        r"|move to|switch to|bump|deprecate)\b",
        re.IGNORECASE,
    )),
    ("exploration", re.compile(
        r"\b(?:how does|what is|explain|show me|walk me through|help me understand"
        r"|tell me about|what are|where is|can you explain|describe)\b",
        re.IGNORECASE,
    )),
]


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _iter_all_text(session: dict) -> Iterator[str]:
    """Yield textual content without duplicating the whole transcript."""

    for msg in session.get("messages", []):
        if msg.get("content"):
            yield msg["content"]
        if msg.get("thinking"):
            yield msg["thinking"]
        for tu in msg.get("tool_uses", []):
            inp = tu.get("input")
            if isinstance(inp, str):
                yield inp
            elif isinstance(inp, dict):
                for v in inp.values():
                    if isinstance(v, str):
                        yield v
            out = tu.get("output")
            if isinstance(out, str):
                yield out
            elif isinstance(out, dict):
                for v in out.values():
                    if isinstance(v, str):
                        yield v


def _iter_text_chunks(text: str, *, size: int = 256 * 1024) -> Iterator[str]:
    """Yield bounded overlapping chunks so regex scans stay memory-stable."""

    overlap = 512
    if len(text) <= size:
        yield text
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        yield text[start:end]
        if end == len(text):
            break
        start = end - overlap


def _iter_all_text_chunks(session: dict) -> Iterator[str]:
    for text in _iter_all_text(session):
        yield from _iter_text_chunks(text)


def _iter_tool_outputs(session: dict) -> Iterator[str]:
    """Yield all tool-use output strings."""

    for msg in session.get("messages", []):
        for tu in msg.get("tool_uses", []):
            out = tu.get("output")
            if isinstance(out, str):
                yield out
            elif isinstance(out, dict):
                for v in out.values():
                    if isinstance(v, str):
                        yield v


def _get_all_tool_uses(session: dict) -> list[dict]:
    """Return a flat list of every tool_use dict in the session."""
    tool_uses: list[dict] = []
    for msg in session.get("messages", []):
        tool_uses.extend(msg.get("tool_uses", []))
    return tool_uses


def _get_user_messages(session: dict) -> list[str]:
    """Return content strings from user messages."""
    return [
        msg["content"]
        for msg in session.get("messages", [])
        if msg.get("role") == "user" and msg.get("content")
    ]


# ---------------------------------------------------------------------------
# Badge functions
# ---------------------------------------------------------------------------

def compute_outcome_badge(session: dict) -> str:
    """Determine the outcome of a session from tool outputs.

    Returns one of: tests_passed, tests_failed, build_failed, analysis_only,
    completed, errored, partial
    """
    tool_uses = _get_all_tool_uses(session)

    if not tool_uses:
        return "analysis_only"

    # Check if all tools are read-only
    tool_names = {tu.get("tool", "") for tu in tool_uses}
    if tool_names and tool_names <= _READ_ONLY_TOOLS:
        return "analysis_only"

    # Check for test results -- scan in priority order (failures trump passes)
    has_test_pass = False
    has_test_fail = False
    has_build_fail = False

    for output in _iter_tool_outputs(session):
        # Build failures (check first so "BUILD FAILED" isn't caught as test failure)
        if re.search(r"BUILD FAILED|build failed", output):
            has_build_fail = True
        if re.search(r"(?:compile|compilation)\s+(?:error|failed)", output, re.IGNORECASE):
            has_build_fail = True
        if re.search(r"error\[E\d+\]", output):  # Rust compiler errors
            has_build_fail = True
        if re.search(r"error TS\d+:", output):  # TypeScript errors
            has_build_fail = True

        # Test failures (exclude lines that are build failures)
        if re.search(r"(?<!BUILD\s)FAILED\s+\S+::", output):
            has_test_fail = True
        if re.search(r"\d+\s+failed", output):
            has_test_fail = True
        if re.search(r"AssertionError|FAIL:|Tests?:\s*\d+\s+failed", output):
            has_test_fail = True
        if re.search(r"FAILURES|failures=\d*[1-9]", output):
            has_test_fail = True

        # Test passes
        if re.search(r"\d+\s+passed", output):
            has_test_pass = True
        if re.search(r"\bpassed\b", output) and re.search(r"pytest|jest|mocha|vitest", output, re.IGNORECASE):
            has_test_pass = True
        if re.search(r"\bOK\b", output) and re.search(r"tests?\s+run|Ran\s+\d+", output, re.IGNORECASE):
            has_test_pass = True
        if re.search(r"Tests?:\s+\d+\s+passed,\s+\d+\s+total", output):
            has_test_pass = True
        if re.search(r"✓|All tests passed|BUILD SUCCESSFUL", output):
            has_test_pass = True

    # Priority: test failures > build failures > test passes
    if has_test_fail:
        return "tests_failed"
    if has_build_fail:
        return "build_failed"
    if has_test_pass:
        return "tests_passed"

    # --- Fallback: no test/build signals detected ---
    # Check for error indicators in the last third of tool outputs
    messages = session.get("messages", [])
    last_third_start = max(len(messages) * 2 // 3, 0)
    has_late_error = False
    for msg in messages[last_third_start:]:
        for tu in msg.get("tool_uses", []):
            out = tu.get("output")
            if isinstance(out, str):
                values = [out]
            elif isinstance(out, dict):
                values = [v for v in out.values() if isinstance(v, str)]
            else:
                values = []
            if any(
                re.search(
                    r"\b(?:Error|Exception|Traceback|FAILED|FATAL|panic|Segmentation fault"
                    r"|Permission denied|No such file|command not found)\b",
                    chunk,
                )
                for value in values
                for chunk in _iter_text_chunks(value)
            ):
                has_late_error = True
                break
        if has_late_error:
            break

    # Check if session appears interrupted (user spoke last, no agent reply)
    last_role = None
    for msg in reversed(messages):
        if msg.get("role") in ("user", "assistant"):
            last_role = msg.get("role")
            break

    if last_role == "user":
        return "partial"
    if has_late_error:
        return "errored"

    return "completed"


def compute_value_badges(session: dict) -> list[str]:
    """Compute value signal badges.

    Possible badges: novel_domain, long_horizon, tool_rich, scientific_workflow, debugging
    """
    badges: list[str] = []
    stats = session.get("stats", {})
    scientific_libraries: set[str] = set()
    has_sci_files = False
    has_sci_signal = False
    for chunk in _iter_all_text_chunks(session):
        scientific_libraries.update(
            match.lower() for match in _SCIENTIFIC_LIBS.findall(chunk)
        )
        has_sci_files = has_sci_files or bool(_SCIENTIFIC_EXTENSIONS.search(chunk))
        has_sci_signal = has_sci_signal or bool(_SCIENTIFIC_TERMS.search(chunk))
        has_sci_signal = has_sci_signal or bool(_SCIENTIFIC_LIBS.search(chunk))

    # novel_domain: specialized/scientific libraries (require 2+ distinct matches)
    if len(scientific_libraries) >= 2:
        badges.append("novel_domain")

    # long_horizon: truly extended sessions (require both many turns AND high tokens)
    user_msgs = stats.get("user_messages", 0)
    total_tokens = stats.get("input_tokens", 0) + stats.get("output_tokens", 0)
    if user_msgs > 20 and total_tokens > 100_000:
        badges.append("long_horizon")

    # tool_rich
    total_msgs = stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
    tool_count = stats.get("tool_uses", 0)
    if tool_count / max(total_msgs, 1) > 0.5:
        badges.append("tool_rich")

    # scientific_workflow: require scientific file extensions AND scientific terms/libs
    if has_sci_files and has_sci_signal:
        badges.append("scientific_workflow")

    # debugging: error -> fix -> verify pattern
    messages = session.get("messages", [])
    if len(messages) >= 3:
        # Split messages into thirds
        third = max(len(messages) // 3, 1)
        has_early_error = any(
            re.search(
                r"\b(?:error|bug|broken|crash|traceback|exception|failing)\b",
                chunk,
                re.IGNORECASE,
            )
            for msg in messages[:third]
            for value in [msg.get("content")]
            if isinstance(value, str)
            for chunk in _iter_text_chunks(value)
        )
        late_values = (
            value
            for msg in messages[third * 2:]
            for value in (
                [msg.get("content")]
                + [tu.get("output") for tu in msg.get("tool_uses", [])]
            )
            if isinstance(value, str)
        )
        has_late_verify = any(
            re.search(
                r"\b(?:passed|works|fixed|resolved|success|OK|verified)\b",
                chunk,
                re.IGNORECASE,
            )
            for value in late_values
            for chunk in _iter_text_chunks(value)
        )
        if has_early_error and has_late_verify:
            badges.append("debugging")

    return badges


def _compute_risk_and_sensitivity(session: dict) -> tuple[list[str], float]:
    """Compute risk badges and sensitivity score in a single pass.

    Scans all text once for secrets, names, and private URLs, then
    derives both badges and the numeric score from the same findings.

    Returns (risk_badges, sensitivity_score).
    """
    secret_count = 0
    name_count = 0
    distinct_names: set[str] = set()
    url_count = 0
    for chunk in _iter_all_text_chunks(session):
        secret_count += len(scan_text(chunk))
        for match in _PROPER_NAME.finditer(chunk):
            name = match.group(0)
            if _NAME_ALLOWLIST.search(name):
                continue
            first_word = name.split()[0]
            if _CODE_PHRASE_PREFIX.match(first_word):
                continue
            distinct_names.add(name.lower())
            name_count += 1
        url_count += len(_PRIVATE_URL.findall(chunk))

    # --- Badges ---
    badges: list[str] = []
    if secret_count > 0:
        badges.append("secrets_detected")
    if len(distinct_names) >= 5:
        badges.append("names_detected")
    if url_count > 0:
        badges.append("private_url")

    # --- Sensitivity score ---
    score = min(
        min(secret_count * 0.3, 1.0)
        + min(name_count * 0.1, 1.0)
        + min(url_count * 0.15, 1.0),
        1.0,
    )

    if score >= 0.7:
        badges.append("manual_review")

    return badges, score


def compute_risk_badges(session: dict) -> list[str]:
    """Compute privacy/sensitivity risk badges.

    Possible badges: secrets_detected, names_detected, private_url, manual_review
    """
    badges, _ = _compute_risk_and_sensitivity(session)
    return badges


def compute_sensitivity_score(session: dict) -> float:
    """Compute a 0.0-1.0 sensitivity score based on findings count and types.

    Higher score = more review needed.
    Weights: secrets (0.3 each, cap at 1.0), names (0.1 each), private_urls (0.15 each)
    """
    _, score = _compute_risk_and_sensitivity(session)
    return score


_TRIVIAL_RE = re.compile(
    r"^(?:/\w+|hello|hi|hey|say hi|say hello|warmup|test|ping"
    r"|list your available tools.*|return x=\d+"
    r"|what number was provided.*)\s*$",
    re.IGNORECASE,
)


def compute_task_type(session: dict) -> str:
    """Infer the task type from conversation content.

    Returns one of: debugging, feature, refactor, analysis, testing,
    documentation, review, configuration, migration, exploration, trivial, unknown
    """
    user_msgs = _get_user_messages(session)
    # Check the first few user messages for intent signals
    text = "\n".join(user_msgs[:5])

    if not text:
        return "trivial"

    # Detect trivial sessions: slash commands, greetings, warmups
    stats = session.get("stats", {})
    total_msgs = stats.get("user_messages", 0) + stats.get("assistant_messages", 0)
    tool_uses = stats.get("tool_uses", 0)
    first_user_msg = user_msgs[0] if user_msgs else ""

    if total_msgs <= 10 and _TRIVIAL_RE.match(first_user_msg.strip()):
        return "trivial"

    # Score each task type by keyword matches, double-weighting the first message
    best_type = "unknown"
    best_score = 0

    for task_type, pattern in _TASK_PATTERNS:
        first_hits = len(pattern.findall(first_user_msg))
        all_hits = len(pattern.findall(text))
        score = all_hits + first_hits  # first msg counted twice
        if score > best_score:
            best_score = score
            best_type = task_type

    return best_type


_SKIP_PATTERNS = [
    re.compile(r"^\s*\[Request interrupted by user\]\s*$"),
    # Single-word terse commands (init, install, exit, help, etc.)
    re.compile(r"^\s*[a-z]{2,12}\s*$"),
]
_XML_TAG_RE = re.compile(r"<[^>]+>")


def _is_skippable_message(text: str) -> bool:
    """Return True if *text* is an internal command or too terse to be a title."""
    cleaned = strip_high_confidence_internal_wrappers(text)
    return not cleaned or any(pattern.match(cleaned) for pattern in _SKIP_PATTERNS)


def compute_display_title(session: dict) -> str:
    """Extract a short display title from the first real user message.

    Skips internal Claude Code command messages (XML-wrapped slash commands,
    local-command wrappers, etc.) and strips XML/HTML tags from the result.
    Truncates to ~80 chars.
    """
    # Prefer a real segment title, but tolerate blobs written by older parsers
    # that selected an internal harness notification as the title.
    seg_title = strip_high_confidence_internal_wrappers(
        session.get("segment_title", "")
    )
    if seg_title and not _is_skippable_message(seg_title):
        if len(seg_title) > 80:
            return seg_title[:77] + "..."
        return seg_title

    fallback = session.get("project", "Untitled session")
    source = session.get("source", "")
    if source and fallback == "Untitled session":
        fallback = f"{source}:{session.get('project', 'unknown')}"

    user_msgs = _get_user_messages(session)
    if not user_msgs:
        return fallback

    # Find the first real user message (skip internal commands)
    text = ""
    for msg in user_msgs:
        cleaned = strip_high_confidence_internal_wrappers(msg)
        if not _is_skippable_message(cleaned):
            text = cleaned.strip()
            break

    if not text:
        return fallback

    # Strip any remaining XML/HTML tags
    text = _XML_TAG_RE.sub("", text).strip()

    if not text:
        return fallback

    # Strip common conversational prefixes
    prefixes = [
        "Can you ", "Could you ", "Please ", "I need you to ", "I want you to ",
        "I'd like you to ", "Help me ", "Let's ", "Let me ",
    ]
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):]
            # Capitalize the first letter after stripping
            if text:
                text = text[0].upper() + text[1:]
            break

    # Take the first line only
    first_line = text.split("\n", 1)[0].strip()

    # Truncate
    if len(first_line) > 80:
        # Try to break at a word boundary
        truncated = first_line[:77]
        last_space = truncated.rfind(" ")
        if last_space > 40:
            truncated = truncated[:last_space]
        first_line = truncated + "..."

    return first_line if first_line else fallback


def compute_all_badges(session: dict) -> dict:
    """Compute all badges and signals for a session.

    Returns dict with keys:
    - display_title: str
    - outcome_badge: str
    - value_badges: list[str]
    - risk_badges: list[str]
    - sensitivity_score: float
    - task_type: str
    - files_touched: list[str]
    - commands_run: list[str]
    """
    # Extract files touched, commands run, and tool counts from tool uses
    files_touched: list[str] = []
    commands_run: list[str] = []
    tool_counts: dict[str, int] = {}
    seen_files: set[str] = set()
    seen_commands: set[str] = set()

    for msg in session.get("messages", []):
        # Count tool uses from inline tool_uses list
        for tu in msg.get("tool_uses", []):
            tool = tu.get("tool", "")
            if tool:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            inp = tu.get("input")

            # Extract file paths
            if isinstance(inp, dict):
                for key in ("file_path", "path", "file", "filename"):
                    val = inp.get(key)
                    if isinstance(val, str) and val not in seen_files:
                        seen_files.add(val)
                        files_touched.append(val)

                # Extract commands
                if tool in ("Bash", "bash", "execute_command", "run_command"):
                    cmd = inp.get("command", "")
                    if isinstance(cmd, str) and cmd and cmd not in seen_commands:
                        seen_commands.add(cmd)
                        commands_run.append(cmd)
            elif isinstance(inp, str):
                # Some tools pass input as a plain string (e.g. command)
                if tool in ("Bash", "bash") and inp not in seen_commands:
                    seen_commands.add(inp)
                    commands_run.append(inp)

        # Count top-level tool entries (clawjournal parsed format)
        top_tool = msg.get("tool")
        if top_tool:
            tool_counts[top_tool] = tool_counts.get(top_tool, 0) + 1

    risk_badges, sensitivity_score = _compute_risk_and_sensitivity(session)

    return {
        "display_title": compute_display_title(session),
        "outcome_badge": compute_outcome_badge(session),
        "value_badges": compute_value_badges(session),
        "risk_badges": risk_badges,
        "sensitivity_score": sensitivity_score,
        "task_type": compute_task_type(session),
        "files_touched": files_touched,
        "commands_run": commands_run,
        "tool_counts": tool_counts,
    }
