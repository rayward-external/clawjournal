"""Local regex worker. Input stays in pipes; output contains offsets only.

Run this file by its installed absolute path. The caller's working directory
may contain another ClawJournal checkout and must not select worker code.
"""
from __future__ import annotations

import json
import re
import sys


def match_starts(pattern, chunks) -> list[int]:
    output = []
    for chunk in chunks:
        source, offset, start, end = chunk
        output.extend(
            offset + match.start()
            for match in pattern.finditer(source)
            if start <= offset + match.start() < end
        )
    return output


def main() -> None:
    request = json.load(sys.stdin)
    pattern = re.compile(request["pattern"], request["flags"])
    json.dump(match_starts(pattern, request["chunks"]), sys.stdout)


if __name__ == "__main__":
    main()
