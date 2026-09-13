"""Conservative literal prefilter for the built-in regex passes.

Aho-Corasick finds necessary markers in one pass. A missing marker can skip
only the exact reviewed pattern (including flags); it never grants an
exemption or changes a candidate's boundaries. Unknown rules always run.
No field text or candidate positions are cached across calls.
"""
from __future__ import annotations

import re

try:
    import ahocorasick
except (ImportError, OSError):  # native extension unavailable: keep all detection
    ahocorasick = None


# Each tuple is an OR: every possible match contains at least one literal.
# Keys intentionally repeat the complete regex and flags. Editing a detector
# without updating this table removes its shortcut, not its detection.
# IGNORECASE rules use punctuation, never case-folded words. In particular,
# Unicode whitespace, decimal digits and Python's special I/S/K case matches
# must not be ruled out by an ASCII-only keyword check.
_PATTERN_LITERALS = {
    re.compile(r"""eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"""): ('eyJ',),
    re.compile(r"""eyJ[A-Za-z0-9_-]{15,}"""): ('eyJ',),
    re.compile(r"""postgres(?:ql)?://[^:]+:[^@\s]+@[^\s\"'`]+"""): ('postgres',),
    re.compile(r"""sk-ant-[A-Za-z0-9_-]{20,}"""): ('sk-ant-',),
    re.compile(r"""sk-[A-Za-z0-9]{40,}"""): ('sk-',),
    re.compile(r"""hf_[A-Za-z0-9]{20,}"""): ('hf_',),
    re.compile(r"""(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{30,}"""): ('ghp_', 'gho_', 'ghs_', 'ghr_'),
    re.compile(r"""github_pat_[A-Za-z0-9_]{20,}"""): ('github_pat_',),
    re.compile(r"""pypi-[A-Za-z0-9_-]{50,}"""): ('pypi-',),
    re.compile(r"""npm_[A-Za-z0-9]{30,}"""): ('npm_',),
    re.compile(r"""\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{24,}(?![A-Za-z0-9])"""): ('sk_', 'pk_', 'rk_'),
    re.compile(r"""\bwhsec_[A-Za-z0-9]{24,}(?![A-Za-z0-9])"""): ('whsec_',),
    re.compile(r"""(?<![A-Za-z0-9\[])AKIA[0-9A-Z]{16}(?![0-9A-Z\]{}])"""): ('AKIA',),
    re.compile(r"""(?:aws_secret_access_key|secret_key)\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?""", re.IGNORECASE): ('=', ':'),
    re.compile(r"""xox[bpsa]-[A-Za-z0-9-]{20,}"""): ('xox',),
    re.compile(r"""https?://(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"""): ('discord.com/api/webhooks/', 'discordapp.com/api/webhooks/'),
    re.compile(r"""-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"""): ('-----BEGIN ',),
    re.compile(r"""(?:--|-)(?:access[_-]?token|auth[_-]?token|api[_-]?key|secret|password|token)[\s=]+([A-Za-z0-9_/+=.-]{8,})""", re.IGNORECASE): ('-',),
    re.compile(r"""(?:SECRET|PASSWORD|TOKEN|API_KEY|AUTH_KEY|ACCESS_KEY|SERVICE_KEY|DB_PASSWORD|SUPABASE_KEY|SUPABASE_SERVICE|ANON_KEY|SERVICE_ROLE)\s*[=]\s*['\"]?([^\s'\"]{6,})['\"]?""", re.IGNORECASE): ('=',),
    re.compile(r"""(?:secret[_-]?key|api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|service[_-]?role[_-]?key|private[_-]?key)\s*[=:]\s*['"]([A-Za-z0-9_/+=.-]{20,})['"]""", re.IGNORECASE): ('=', ':'),
    re.compile(r"""[?&](?:key|token|secret|password|apikey|api_key|access_token|auth)=([A-Za-z0-9_/+=.-]{8,})""", re.IGNORECASE): ('?', '&'),
    re.compile(r"""\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"""): ('@',),
    re.compile(r"""['"][A-Za-z0-9_/+=.-]{40,}['"]"""): ('"', "'"),
    re.compile(r"""github\.com/([A-Za-z0-9_.-]{2,})"""): ('github.com/',),
    re.compile(r"""raw\.githubusercontent\.com/([A-Za-z0-9_.-]{2,})"""): ('raw.githubusercontent.com/',),
    re.compile(r"""([A-Za-z0-9_.+-]{3,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"""): ('@',),
    re.compile(r"""([A-Za-z0-9_.+-]{3,})@(?=\s|$)"""): ('@',),
    re.compile(r"""(\d{8,}:[A-Za-z0-9_-]{30,})"""): (':',),
    re.compile(r"""\b([a-z][a-z0-9]*s?-(?:macbook|imac|laptop|desktop|pc|workstation|server)-?[a-z0-9]*)\b"""): ('-macbook', '-imac', '-laptop', '-desktop', '-pc', '-workstation', '-server'),
    re.compile(r"""(/(?:Users|home)/[A-Za-z0-9._-]{2,}/[^\s\"'`,;)}\]]{3,})"""): ('/Users/', '/home/'),
    re.compile(r"""\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"""): ('10.',),
    re.compile(r"""\b(172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"""): ('172.',),
    re.compile(r"""\b(192\.168\.\d{1,3}\.\d{1,3})\b"""): ('192.168.',),
    re.compile(r"""\b((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})\b"""): (':', '-'),
    re.compile(r"""\b((?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4})\b"""): ('-',),
    re.compile(r"""(?<!\d)(\+\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{2,4}[-.\s]?\d{3,9})(?!\d)"""): ('+',),
    re.compile(r"""\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*\.(?:local|internal|corp|lan|intranet|localnet))\b""", re.IGNORECASE): ('.',),
    re.compile(r"""(https?://(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?::\d{2,5})?(?:/[^\s\"'`<>]*)?)"""): ('http://', 'https://'),
    re.compile(r"""(https?://(?:localhost|127\.0\.0\.1)(?::\d{2,5})?(?:/[^\s\"'`<>]*)?)"""): ('http://', 'https://'),
    re.compile(r"""(?<![A-Za-z0-9])(@[a-z0-9][a-z0-9-]{1,38}/[a-z0-9][a-z0-9._-]{1,63})\b"""): ('@',),
    re.compile(r"""(?<![A-Za-z0-9@])([a-z][a-z0-9-]{2,}(?:/[a-z][a-z0-9-]+)?@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b"""): ('@',),
    re.compile(r"""github\.com/([A-Za-z0-9._-]{2,39}/[A-Za-z0-9._-]{1,100})(?![A-Za-z0-9])"""): ('github.com/',),
    re.compile(r"""gitlab\.com/([A-Za-z0-9._-]{2,39}(?:/[A-Za-z0-9._-]+){1,4})(?![A-Za-z0-9])"""): ('gitlab.com/',),
    re.compile(r"""bitbucket\.org/([A-Za-z0-9._-]{2,39}/[A-Za-z0-9._-]+)(?![A-Za-z0-9])"""): ('bitbucket.org/',),
    re.compile(r"""(arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s\"'`<>]+)"""): ('arn:aws:',),
    re.compile(r"""\b(\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com)\b"""): ('.dkr.ecr.',),
    re.compile(r"""projects/([a-z][a-z0-9-]{4,28}[a-z0-9])(?![A-Za-z0-9])"""): ('projects/',),
}


def _build_index():
    if ahocorasick is None:
        return None, {}, ()
    markers = sorted({word for words in _PATTERN_LITERALS.values() for word in words})
    bits = {word: 1 << index for index, word in enumerate(markers)}
    automaton = ahocorasick.Automaton()
    for word, bit in bits.items():
        if len(word) > 1:
            automaton.add_word(word, bit)
    automaton.make_automaton()
    return automaton, {
        pattern: sum(bits[word] for word in words)
        for pattern, words in _PATTERN_LITERALS.items()
    }, tuple((word, bit) for word, bit in bits.items() if len(word) == 1)


try:
    _AUTOMATON, _PATTERN_MASKS, _SINGLE_LITERALS = _build_index()
except Exception:
    # This is an optional accelerator, even when installed as a dependency.
    # Failure to build it must not abort ingestion or suppress a detector.
    _AUTOMATON, _PATTERN_MASKS, _SINGLE_LITERALS = None, {}, ()

# Small fields cost more to dispatch through the accelerator than to scan.
_MIN_PREFILTER_CHARS = 256


def filter_rules(text: str, rules):
    """Keep original ordered rule tuples whose required marker may exist.

    The regex remains responsible for validation, case, Unicode semantics and
    offsets. Materialize marker presence before filtering; an iterator error
    therefore falls back to all rules without partial results or duplicates.
    """
    if len(text) < _MIN_PREFILTER_CHARS or _AUTOMATON is None:
        return rules
    # Presence of common punctuation is cheaper than emitting a Python
    # callback tuple for every dot, quote and colon in a large JSON field.
    found = sum(mask for word, mask in _SINGLE_LITERALS if word in text)
    try:
        for _, mask in _AUTOMATON.iter(text):
            found |= mask
    except Exception:
        return rules
    return [row for row in rules
            if (required := _PATTERN_MASKS.get(row[1])) is None or required & found]
