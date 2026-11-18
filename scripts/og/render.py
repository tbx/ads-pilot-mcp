"""Render site/og-image.jpg (1200x630) from scripts/og/template.html.

The headline is read from the English <h1> in site/index.html, so the share
image always matches the page. Needs Google Chrome; on macOS `sips` turns the
screenshot into a JPEG.

Run after changing the hero headline:  uv run python scripts/og/render.py
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
OUT = SITE / "og-image.jpg"
CHROMES = [
    os.environ.get("OG_CHROME"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def headline() -> str:
    page = (SITE / "index.html").read_text(encoding="utf-8")
    h1 = re.search(r"<h1>(.*?)</h1>", page, re.S)
    en = re.search(r'<span lang="en">(.*?)</span>', h1.group(1), re.S) if h1 else None
    if not en:
        sys.exit("No English <h1> found in site/index.html")
    return html.unescape(" ".join(en.group(1).split()))


def main() -> None:
    chrome = next((c for c in CHROMES if c and Path(c).exists()), None)
    if not chrome:
        sys.exit("Chrome not found. Set OG_CHROME=/path/to/chrome")

    text = headline()
    # Line break after the comma, like the hero on a wide screen.
    shown = html.escape(text).replace(", ", ",<br>", 1)
    page = (
        (ROOT / "scripts/og/template.html").read_text(encoding="utf-8")
        .replace("{{archivo}}", (SITE / "fonts/archivo-latin-wdth-normal.woff2").as_uri())
        .replace("{{hero}}", (SITE / "hero.jpg").as_uri())
        .replace("{{logo}}", (SITE / "logo.svg").as_uri())
        .replace("{{headline}}", shown)
    )

    with tempfile.TemporaryDirectory() as work:
        page_file, png = Path(work, "og.html"), Path(work, "og.png")
        page_file.write_text(page, encoding="utf-8")
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
             "--window-size=1200,630", f"--user-data-dir={work}/profile", f"--screenshot={png}", page_file.as_uri()],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if shutil.which("sips"):
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "84", str(png), "--out", str(OUT)],
                           check=True, stdout=subprocess.DEVNULL)
        else:
            shutil.copy(png, OUT.with_suffix(".png"))
            sys.exit(f"sips not available: wrote {OUT.with_suffix('.png')} instead, convert it to JPEG.")
    print(f"{OUT.relative_to(ROOT)}: \"{text}\"")


if __name__ == "__main__":
    main()
