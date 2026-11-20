"""Regenerate the README screenshots and demo animation.

    pip install playwright        # uses your installed Google Chrome, no browser download
    python docs/capture.py

Starts the web app on a free port with a throwaway database, drives it with Playwright,
and writes docs/images/*.png, docs/images/demo.gif and docs/images/demo.webm.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "docs" / "images"
FIX = ROOT / "tests" / "fixtures"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_app(db: str, port: int) -> None:
    import logging

    from werkzeug.serving import make_server

    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    from unveil.web import create_app

    srv = make_server("127.0.0.1", port, create_app(db_path=db), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------------------
# Terminal screenshot: render real CLI output (ANSI colours) as a terminal window
# ---------------------------------------------------------------------------------------

ANSI = {"1": "font-weight:700", "2": "opacity:.62", "31": "color:#ff6b7d", "32": "color:#4fd189", "33": "color:#f2c14e",
        "36": "color:#5fd0e6", "97": "color:#fff", "30": "color:#0e1015", "41": "background:#c81e33",
        "42": "background:#4fd189", "43": "background:#f2c14e"}


def ansi_to_html(text: str) -> str:
    import html
    out, open_spans = [], 0
    for part in re.split(r"(\x1b\[[0-9;]*m)", text):
        m = re.fullmatch(r"\x1b\[([0-9;]*)m", part)
        if not m:
            out.append(html.escape(part))
            continue
        codes = [c for c in m.group(1).split(";") if c]
        if not codes or codes == ["0"]:
            out.append("</span>" * open_spans)
            open_spans = 0
        else:
            out.append('<span style="%s">' % ";".join(ANSI.get(c, "") for c in codes))
            open_spans += 1
    return "".join(out) + "</span>" * open_spans


def cli_output() -> str:
    env = {**os.environ, "UNVEIL_HOME": tempfile.mkdtemp(), "FORCE_TTY": "1"}
    code = (
        "import sys; sys.stdout.isatty = lambda: True; from unveil.cli import main; "
        "sys.exit(main(['clean', 'tests/fixtures/resume_attack.pdf', '-s', 'jordan.example@mail.com', '-o', sys.argv[1]]))"
    )
    out_dir = tempfile.mkdtemp()
    r = subprocess.run([sys.executable, "-c", code, out_dir], cwd=ROOT, env=env, capture_output=True, text=True)
    text = r.stdout.replace(out_dir + "/", "./safe/")
    lines = text.splitlines()
    # keep it readable: drop long quoted continuation lines beyond the first per finding
    trimmed = []
    for ln in lines:
        if "│" in ln and len(ln) > 110:
            ln = ln[:108] + "…"
        trimmed.append(ln)
    return "$ unveil clean resume_attack.pdf -s jordan.example@mail.com\n" + "\n".join(trimmed).strip("\n")


TERMINAL_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;background:transparent;padding:28px;font-family:-apple-system,sans-serif}
.win{background:#0e1015;border-radius:12px;box-shadow:0 20px 50px rgba(0,0,0,.35);overflow:hidden;width:980px;border:1px solid #262b35}
.bar{height:34px;background:#1b1e26;display:flex;align-items:center;gap:8px;padding:0 14px}
.bar i{width:12px;height:12px;border-radius:50%;display:inline-block}
.bar b{color:#8e96a4;font-weight:500;font-size:12.5px;margin-left:10px}
pre{margin:0;padding:18px 20px 22px;color:#d7dbe3;font:13px/1.55 "SF Mono",Menlo,monospace;white-space:pre-wrap}
</style></head><body><div class="win"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i>
<i style="background:#28c840"></i><b>unveil — zsh</b></div><pre>%s</pre></div></body></html>"""


# ---------------------------------------------------------------------------------------

def save_gif(frames: list[tuple[bytes, int]], path: Path, width: int = 960) -> None:
    imgs, durations = [], []
    for png, ms in frames:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if im.width != width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        imgs.append(im)
        durations.append(ms)
    pal = imgs[0].quantize(colors=200, method=Image.Quantize.MEDIANCUT)
    q = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in imgs]
    q[0].save(path, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=True, disposal=1)


def main() -> None:
    from playwright.sync_api import sync_playwright

    IMG.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp()
    port = free_port()
    start_app(os.path.join(tmp, "demo.db"), port)
    base = f"http://127.0.0.1:{port}"
    time.sleep(0.5)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")

        # ---- Animated walkthrough (GIF + WebM video) --------------------------------
        vid_dir = os.path.join(tmp, "video")
        ctx = browser.new_context(viewport={"width": 1200, "height": 760}, device_scale_factor=1,
                                  color_scheme="light", record_video_dir=vid_dir,
                                  record_video_size={"width": 1200, "height": 760})
        page = ctx.new_page()
        frames: list[tuple[bytes, int]] = []
        shot = lambda ms, **kw: frames.append((page.screenshot(**kw), ms))  # noqa: E731

        page.goto(base)
        page.wait_for_timeout(400)
        shot(1600)
        page.set_input_files("#file", str(FIX / "resume_attack.pdf"))
        page.wait_for_timeout(250)
        shot(900)
        page.click("#sender")
        for i, ch in enumerate("jordan.example@mail.com"):
            page.keyboard.type(ch)
            if i % 6 == 5:
                shot(110)
        page.wait_for_timeout(250)
        shot(900)
        page.evaluate("document.getElementById('ov-title').textContent='Scanning 1 file…';"
                      "document.getElementById('overlay').classList.add('on')")
        page.wait_for_timeout(300)
        shot(1100)
        page.evaluate("document.getElementById('overlay').classList.remove('on')")
        page.click("#go")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        result_url = page.url
        shot(2600)
        for y in (380, 760, 1140):
            page.mouse.wheel(0, 380)
            page.wait_for_timeout(450)
            shot(1300)
        page.locator("text=Where it was hidden").scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        shot(3000)
        page.evaluate("window.scrollTo(0,0)")
        page.click("text=Block sender")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(400)
        shot(2200)
        page.goto(base + "/senders")
        page.wait_for_timeout(500)
        shot(2600)
        video_path = page.video.path()
        ctx.close()
        save_gif(frames, IMG / "demo.gif")
        shutil.move(video_path, IMG / "demo.webm")

        # Populate more history for the static shots
        page = browser.new_page()
        for files, sender in [(["essay_attack.docx", "essay_clean.docx"], "student42@uni.edu"),
                              (["resume_clean.pdf"], "alex@careers.dev"),
                              (["portfolio_attack.html"], "sam.r@example.org")]:
            page.goto(base)
            page.set_input_files("#file", [str(FIX / f) for f in files])
            page.fill("#sender", sender)
            page.click("#go")
            page.wait_for_load_state("networkidle")
        page.close()

        # ---- Static screenshots ------------------------------------------------------
        for scheme in ("light", "dark"):
            ctx = browser.new_context(viewport={"width": 1280, "height": 820}, device_scale_factor=2, color_scheme=scheme)
            page = ctx.new_page()
            sfx = "" if scheme == "light" else "-dark"
            page.goto(base)
            page.set_input_files("#file", [str(FIX / "resume_attack.pdf"), str(FIX / "essay_attack.docx")])
            page.fill("#sender", "jordan.example@mail.com")
            page.wait_for_timeout(300)
            page.screenshot(path=IMG / f"home{sfx}.png")
            page.goto(result_url)
            page.wait_for_timeout(400)
            page.screenshot(path=IMG / f"result{sfx}.png")
            if scheme == "light":
                page.add_style_tag(content=".topbar{position:static}")
                page.locator("section.card:has-text('Where it was hidden')").screenshot(path=IMG / "compare.png")
                page.goto(base + "/senders")
                page.wait_for_timeout(300)
                page.screenshot(path=IMG / "senders.png")
                mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                             color_scheme="light", is_mobile=True)
                mp = mobile.new_page()
                mp.goto(result_url)
                mp.wait_for_timeout(300)
                mp.screenshot(path=IMG / "mobile.png")
                mobile.close()
            ctx.close()

        # ---- CLI ---------------------------------------------------------------------
        page = browser.new_page(device_scale_factor=2, viewport={"width": 1040, "height": 400})
        page.set_content(TERMINAL_HTML.replace("%s", ansi_to_html(cli_output())))
        page.locator(".win").screenshot(path=IMG / "cli.png", omit_background=True)
        browser.close()

    for f in sorted(IMG.iterdir()):
        print(f"{f.name:16} {f.stat().st_size / 1e3:8.0f} KB")


if __name__ == "__main__":
    main()
