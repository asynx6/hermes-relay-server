"""Sanitize untrusted text before printing it to the terminal.

Message content comes from other agents (and, via the LLM, from public input).
Without sanitization, an attacker could inject ANSI escape sequences to forge
log lines, hide text, or scramble the terminal display. The functions here are
for **display** only — the original data stays intact in the database.
"""
import re

# CSI/OSC/ANSI escapes + other control characters (except tab and newline).
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MAX_DISPLAY = 500


def safe_text(value: str | None, max_len: int = MAX_DISPLAY) -> str:
    """Clean text for display: strip ANSI + control chars, cap the length."""
    if not value:
        return ""
    text = _ANSI.sub("", value)
    text = _CONTROL.sub("", text)
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text
