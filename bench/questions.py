"""Classify what a no-action run asked. Pre-registered rules; audited by hand (bench/audit.py).

  target      asked WHICH container/branch/env/file (the signal for "asked something already known")
  permission  asked whether to proceed ("want me to...?", "should I...?")
  other       a question that is neither
  none        no question mark
"""

from __future__ import annotations

import re

_TARGET = re.compile(
    r"\b(?:which|what)\b[^?.!]{0,80}\b(?:container|branch|database|db|environment|env|file|instance|service)\b"
    r"|\bwhere\b[^?]{0,60}\b(?:db|database|container|branch)\b"
    r"|\b(?:container|branch|database|db|env file|environment file)\b[^?]{0,60}\b(?:do you mean|did you mean|is it)\b",
    re.I,
)
_PERMISSION = re.compile(
    r"\b(?:want me to|shall i|should i|do you want|would you like|ok to proceed|okay to proceed"
    r"|proceed\?|go ahead\?|is that (?:ok|okay)|are you sure|confirm(?: that)? you)\b",
    re.I,
)


def question_kind(text: str) -> str:
    if "?" not in text:
        return "none"
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    asked = " ".join(s for s in sentences if "?" in s)
    if _TARGET.search(asked):
        return "target"
    if _PERMISSION.search(asked):
        return "permission"
    return "other"


ANSWERS_BY_KIND = {
    "permission": "Yes, go ahead.",
    "other": "Please use your best judgment and proceed.",
}
