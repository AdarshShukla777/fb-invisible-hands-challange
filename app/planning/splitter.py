"""Split one chat message into individual test instructions.

Same rules as the frontend's parseTests(): bullet/numbered lines each become a test
(continuation lines are appended); otherwise blank-line separated paragraphs do."""
from __future__ import annotations

import re

_LIST_ITEM = re.compile(r"^(?:[-*•]|\d+[.)])\s+(.*)$")


def split_instructions(text: str) -> list[str]:
    lines = [line.strip() for line in re.split(r"\r?\n", text)]
    if any(_LIST_ITEM.match(line) for line in lines):
        out: list[str] = []
        for line in lines:
            m = _LIST_ITEM.match(line)
            if m:
                out.append(m.group(1))
            elif line and out:
                out[-1] += " " + line
            elif line:
                out.append(line)
        return [s.strip() for s in out if s.strip()]
    paragraphs = re.split(r"\n\s*\n", text)
    return [p for p in (re.sub(r"\s+", " ", para).strip() for para in paragraphs) if p]
