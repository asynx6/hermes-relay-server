"""Sanitasi teks tidak tepercaya sebelum ditampilkan (cegah ANSI injection)."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "hermes-agent"))

from hermes.safe_text import safe_text  # noqa: E402


def test_strips_ansi_color_sequence():
    payload = "\x1b[31mPALSU\x1b[0m pesan asli"
    assert safe_text(payload) == "PALSU pesan asli"


def test_strips_osc_title_sequence():
    payload = "\x1b]0;pwned\x07lanjut"
    assert safe_text(payload) == "lanjut"


def test_strips_control_chars_but_keeps_tab_and_newline():
    payload = "baris1\nbaris2\ttab\x07\x08"
    assert safe_text(payload) == "baris1\nbaris2\ttab"


def test_truncates_long_text():
    assert safe_text("a" * 600).endswith("…")
    assert len(safe_text("a" * 600)) == 501


def test_none_and_empty():
    assert safe_text(None) == ""
    assert safe_text("") == ""
