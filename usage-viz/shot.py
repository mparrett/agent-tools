# /// script
# dependencies = ["playwright"]
# ///
"""Render check for usage-viz pages: light, dark, and phone-width screenshots,
failing on console errors or horizontal overflow.

  uv run usage-viz/shot.py OUT_DIR page.html [...]
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out, pages = Path(sys.argv[1]), [Path(p).resolve() for p in sys.argv[2:]]
out.mkdir(parents=True, exist_ok=True)
bad = False
with sync_playwright() as p:
    b = p.chromium.launch()
    for page in pages:
        for name, vp, scheme in [
            ("light", (1120, 900), "light"),
            ("dark", (1120, 900), "dark"),
            ("mobile", (390, 800), "light"),
        ]:
            pg = b.new_page(viewport={"width": vp[0], "height": vp[1]}, color_scheme=scheme)
            errs = []
            # Bind this page's list: a late error from a page still open would
            # otherwise land in whichever page's list is current when it fires.
            pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
            pg.on("console", lambda m, errs=errs: m.type == "error" and errs.append(m.text))
            pg.goto(page.as_uri())
            pg.wait_for_timeout(300)
            pg.mouse.move(vp[0] * 0.8, 420)
            pg.wait_for_timeout(100)
            shot = out / f"{page.stem}-{name}.png"
            pg.screenshot(path=shot, full_page=True)
            overflow = pg.evaluate("document.documentElement.scrollWidth") > vp[0]
            bad |= bool(errs) or overflow
            print(f"{shot}  errors={errs or 'none'}  overflow={overflow}")
            pg.close()
    b.close()
sys.exit(1 if bad else 0)
