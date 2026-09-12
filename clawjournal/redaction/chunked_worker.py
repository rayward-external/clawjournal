"""Local regex worker. Input stays in pipes; output contains offsets only.

Use a module subprocess instead of multiprocessing's main-module import:
ClawJournal also runs from hooks, embedded callers and unguarded scripts.
"""
from __future__ import annotations

import json
import re
import sys


def main() -> None:
    request = json.load(sys.stdin)
    pattern = re.compile(request["pattern"], request["flags"])
    output = []
    for chunk in request["chunks"]:
        source, offset, start, end = chunk
        output.extend(
            offset + match.start()
            for match in pattern.finditer(source)
            if start <= offset + match.start() < end
        )
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
