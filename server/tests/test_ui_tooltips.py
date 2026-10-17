"""Every button in the SPA explains itself with a tooltip (title)."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def test_every_button_in_the_page_has_a_title():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    tags = re.findall(r"<button\b[^>]*>", html) + re.findall(r'<(?:a|label)\b[^>]*class="button-like"[^>]*>', html)
    missing = [t for t in tags if not re.search(r'\stitle="[^"]{3,}"', t)]
    assert tags and not missing, missing


def test_buttons_created_in_javascript_get_a_title():
    missing = []
    for js in WEB.rglob("*.js"):
        lines = js.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if 'createElement("button")' in line:
                window = "\n".join(lines[i:i + 12])
                if ".title =" not in window:
                    missing.append(f"{js.relative_to(WEB)}:{i + 1}")
    assert not missing, missing
