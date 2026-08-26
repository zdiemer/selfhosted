#!/usr/bin/env python3
"""Renders a command's real terminal output to a PNG for the README.

Runs the command with a fixed width and colour forced on, parses the ANSI
escapes, and paints them into a terminal window. Nothing is retyped or faked —
what you see in the image is what the command printed.

    pip install playwright && playwright install chromium

    python3 docs/capture/termshot.py nodes    --head 14 -- kubectl get nodes
    python3 docs/capture/termshot.py releases --head 30 -- helm list -A

Writes docs/shots/<name>.png.
"""
import argparse
import html
import os
import re
import subprocess
import sys

from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shots")
COLUMNS = 104

# A warm scheme that sits with the romnas banner rather than fighting it.
BG, FG, DIM = "#0f1620", "#e6ecf3", "#7e8fa3"
ANSI = {
    30: "#3b3630", 31: "#e06c5e", 32: "#8fbf6a", 33: "#e0a94a",
    34: "#6b9fd4", 35: "#c08ad0", 36: "#5fb8ac", 37: "#d8d2c6",
    90: "#6d6558", 91: "#f0897a", 92: "#a9d488", 93: "#f0c46a",
    94: "#8bb8e4", 95: "#d6a6e4", 96: "#7fd0c4", 97: "#f4f0e8",
}
SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]")  # every final byte but "m"


def ansi_to_html(text):
    """Turn SGR-coloured text into spans. Unsupported escapes are dropped."""
    text = OTHER_CSI.sub("", text.replace("\r\n", "\n"))
    out, pos, style, depth = [], 0, {}, 0

    def open_span():
        bits = []
        if "fg" in style:
            bits.append(f"color:{style['fg']}")
        if style.get("bold"):
            bits.append("font-weight:700")
        if style.get("dim"):
            bits.append("opacity:.62")
        if style.get("italic"):
            bits.append("font-style:italic")
        if style.get("under"):
            bits.append("text-decoration:underline")
        return f'<span style="{";".join(bits)}">' if bits else "<span>"

    for m in SGR.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        pos = m.end()
        codes = [int(c) for c in (m.group(1) or "0").split(";") if c != ""] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                style = {}
            elif c == 1:
                style["bold"] = True
            elif c == 2:
                style["dim"] = True
            elif c == 3:
                style["italic"] = True
            elif c == 4:
                style["under"] = True
            elif c == 22:
                style.pop("bold", None); style.pop("dim", None)
            elif c == 39:
                style.pop("fg", None)
            elif c in ANSI:
                style["fg"] = ANSI[c]
            elif c == 38 and i + 1 < len(codes):
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    n = codes[i + 2]; i += 2
                    if n < 8:
                        style["fg"] = ANSI.get(30 + n, FG)
                    elif n < 16:
                        style["fg"] = ANSI.get(82 + n, FG)
                    elif n < 232:
                        n -= 16
                        r, g, b = (n // 36) * 51, ((n // 6) % 6) * 51, (n % 6) * 51
                        style["fg"] = f"rgb({r},{g},{b})"
                    else:
                        v = 8 + (n - 232) * 10
                        style["fg"] = f"rgb({v},{v},{v})"
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    style["fg"] = f"rgb({codes[i+2]},{codes[i+3]},{codes[i+4]})"; i += 4
            i += 1
        out.append("</span>" * depth)
        depth = 0
        out.append(open_span())
        depth = 1
    out.append(html.escape(text[pos:]))
    out.append("</span>" * depth)
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--head", type=int, help="keep only the first N lines")
    ap.add_argument("--tail", type=int, help="keep only the last N lines")
    ap.add_argument("--title", help="text in the window title bar")
    argv = sys.argv[1:]
    if "--" not in argv:
        sys.exit("no command given (put it after --)")
    split = argv.index("--")
    a = ap.parse_args(argv[:split])
    cmd = argv[split + 1:]
    if not cmd:
        sys.exit("no command given (put it after --)")

    env = dict(os.environ, COLUMNS=str(COLUMNS), FORCE_COLOR="1", TERM="xterm-256color")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, env=env)
    text = proc.stdout.rstrip("\n")
    lines = text.split("\n")
    if a.head:
        lines, text = lines[:a.head], None
    if a.tail:
        lines = lines[-a.tail:]
    text = "\n".join(lines)

    title = a.title or "$ " + " ".join(cmd)
    body = ansi_to_html(text)
    page_html = f"""<!doctype html><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:transparent;padding:26px;display:inline-block}}
.win{{background:{BG};border:1px solid #ffc61c2e;border-radius:12px;overflow:hidden;
     box-shadow:0 18px 50px #00000075;display:inline-block}}
.bar{{display:flex;align-items:center;gap:8px;padding:11px 15px;background:#16202c;
     border-bottom:1px solid #ffc61c1f}}
.dot{{width:11px;height:11px;border-radius:50%;opacity:.85}}
.t{{flex:1;text-align:center;font:500 12.5px/1 'DejaVu Sans Mono',monospace;
   color:{DIM};letter-spacing:.02em;margin-right:36px}}
pre{{padding:20px 24px 22px;font:14px/1.52 'DejaVu Sans Mono',monospace;color:{FG};
    white-space:pre;tab-size:4}}
</style>
<div class="win">
  <div class="bar">
    <span class="dot" style="background:#ff5f57"></span>
    <span class="dot" style="background:#febc2e"></span>
    <span class="dot" style="background:#28c840"></span>
    <span class="t">{html.escape(title)}</span>
  </div>
  <pre>{body}</pre>
</div>"""

    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, f"{a.name}.png")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_context(viewport={"width": 1200, "height": 400},
                           device_scale_factor=2).new_page()
        pg.set_content(page_html, wait_until="load")
        pg.locator(".win").screenshot(path=dest, omit_background=True)
        b.close()
    print(f"{a.name:<10} {dest}")


if __name__ == "__main__":
    main()
