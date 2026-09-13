"""Generate positive witnesses from live regexes, independently of the hints.

The generator is test-only. Every witness is checked by Python's real engine
before it may test the necessary-marker invariant (no vacuous positives).
"""
import random
import re

import pytest

try:
    from re import _parser as parser
except ImportError:  # Python 3.10
    import sre_parse as parser

from clawjournal.redaction import pii, prefilter, secrets


LIVE = dict((pattern, name) for name, pattern in secrets.SECRET_PATTERNS)
LIVE.update((row[1], row[0]) for row in pii._PII_CONTENT_PATTERNS_COMPILED)
HINTED = [pattern for pattern in LIVE if pattern in prefilter._PATTERN_LITERALS]


def generate(tree, rng, flags):
    parts = []
    for op, arg in tree:
        name = str(op)
        if name == 'LITERAL':
            char = chr(arg)
            if flags & re.I and char.isalpha():
                char = rng.choice((char, char.upper(), char.lower()))
            parts.append(char)
        elif name == 'NOT_LITERAL':
            parts.append(next(c for c in 'Z7x/' if ord(c) != arg))
        elif name == 'IN':
            if str(arg[0][0]) == 'NEGATE':
                alphabet = 'Za7/._-:=!?\u2003中'
                def accepts(c):
                    return not any(
                        (str(o) == 'LITERAL' and ord(c) == a)
                        or (str(o) == 'RANGE' and a[0] <= ord(c) <= a[1])
                        or (str(o) == 'CATEGORY' and c.isspace())
                        for o, a in arg[1:]
                    )
                parts.append(rng.choice([c for c in alphabet if accepts(c)]))
            else:
                parts.append(generate([rng.choice(arg)], rng, flags))
        elif name == 'RANGE':
            parts.append(chr(rng.randint(*arg)))
        elif name == 'CATEGORY':
            parts.append(rng.choice({
                'CATEGORY_DIGIT': '019٢９', 'CATEGORY_SPACE': ' \t\n\u2003',
                'CATEGORY_WORD': 'aZ19_中', 'CATEGORY_NOT_SPACE': 'a9_-',
            }[str(arg)]))
        elif name == 'BRANCH':
            parts.append(generate(rng.choice(arg[1]), rng, flags))
        elif name == 'SUBPATTERN':
            parts.append(generate(arg[-1], rng, (flags | arg[1]) & ~arg[2]))
        elif name in {'MAX_REPEAT', 'MIN_REPEAT', 'POSSESSIVE_REPEAT'}:
            low, high, child = arg
            for _ in range(rng.randint(low, min(high, low + 8))):
                parts.append(generate(child, rng, flags))
        elif name == 'ANY':
            parts.append(rng.choice('Az9/'))
        elif name in {'AT', 'ASSERT', 'ASSERT_NOT'}:
            pass  # Real search below must validate all assertions.
        else:
            raise AssertionError(f'Extend the generator for {name}')
    return ''.join(parts)


def witnesses(pattern):
    rng = random.Random(225)
    tree = parser.parse(pattern.pattern, pattern.flags)
    accepted = 0
    for _ in range(3200):
        value = generate(tree, rng, pattern.flags)
        match = pattern.search(value)
        if match is None:
            continue
        yield match.group()
        accepted += 1
        if accepted == 320:
            return
    raise AssertionError(f'Only {accepted} valid witnesses for {pattern.pattern}')


def test_every_hint_is_attached_to_a_live_rule():
    assert prefilter._PATTERN_LITERALS.keys() <= LIVE.keys()


@pytest.mark.parametrize('pattern', HINTED, ids=[f'{LIVE[p]}-{i}' for i, p in enumerate(HINTED)])
def test_generated_positives_cannot_be_rejected(pattern):
    literals = prefilter._PATTERN_LITERALS[pattern]
    rules = [('live', pattern)]
    for value in witnesses(pattern):
        assert any(literal in value for literal in literals), (pattern.pattern, value)
        assert prefilter.filter_rules(' ' * 300 + value, rules) == rules


def test_aws_narrowing_mutation_is_detected():
    pattern = next(p for name, p in secrets.SECRET_PATTERNS if 'AKIA' in p.pattern)
    assert any('AKIAA' not in value for value in witnesses(pattern))


LONG_MARKERS = [(pattern, word) for pattern in HINTED for word in prefilter._PATTERN_LITERALS[pattern] if len(word) > 1]


@pytest.mark.parametrize('pattern,word', LONG_MARKERS, ids=[f'marker-{i}' for i in range(len(LONG_MARKERS))])
def test_narrowing_any_multicharacter_marker_is_caught(monkeypatch, pattern, word):
    changed = tuple(marker + 'A' if marker == word else marker for marker in prefilter._PATTERN_LITERALS[pattern])
    monkeypatch.setitem(prefilter._PATTERN_LITERALS, pattern, changed)
    with pytest.raises(AssertionError):
        test_generated_positives_cannot_be_rejected(pattern)
