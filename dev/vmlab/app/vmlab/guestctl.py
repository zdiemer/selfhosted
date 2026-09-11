"""Type at a guest and look at its screen — the two things needed to drive an
installer without a human in front of it.

Run inside the vmlab pod, which is the only thing NetworkPolicy lets reach a
session's monitor and display:

    python -m vmlab.guestctl shot   <slug> [out.png]
    python -m vmlab.guestctl keys   <slug> ret ret f8 ...
    python -m vmlab.guestctl type   <slug> "some text"
    python -m vmlab.guestctl click  <slug> <x> <y> [width height]
    python -m vmlab.guestctl wait   <slug> [--stable 3] [--timeout 600]

`wait` blocks until the screen stops changing, which is the honest way to know
an installer has finished a step: there is no other signal from outside.

This ships in the image rather than living in a scratch directory because
driving a guest is a recurring operational need — reinstalling Windows XP is
not a one-off — and because it belongs next to the QMP and RFB clients it uses.
"""

from __future__ import annotations

import hashlib
import sys
import time

from vmlab import k8s, qmp, screenshot

# QEMU's sendkey names differ from what you would type. Only the ones an
# installer actually needs are mapped; anything unmapped is passed through, so
# `ret`, `f8`, `kp_enter` and friends work as-is.
TEXT_KEYS = {
    " ": "spc", "-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right",
    ";": "semicolon", "'": "apostrophe", "`": "grave_accent", "\\": "backslash",
    ",": "comma", ".": "dot", "/": "slash", "\n": "ret", "\t": "tab",
}
SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "minus", "+": "equal", "{": "bracket_left",
    "}": "bracket_right", ":": "semicolon", '"': "apostrophe", "~": "grave_accent",
    "|": "backslash", "<": "comma", ">": "dot", "?": "slash",
}


def _ip(slug: str) -> str:
    session = k8s._running_session(slug)
    if session is None:
        raise SystemExit(f"{slug} is not running")
    return session["podIP"]


def keys(slug: str, names: list[str], delay: float = 0.25) -> None:
    ip = _ip(slug)
    with qmp.Qmp(ip) as conn:
        for name in names:
            out = conn.hmp(f"sendkey {name}")
            if out:
                raise SystemExit(f"sendkey {name}: {out}")
            time.sleep(delay)


def type_text(slug: str, text: str, delay: float = 0.06) -> None:
    """Type a literal string. Shifted characters are sent as `shift-<key>`."""
    ip = _ip(slug)
    with qmp.Qmp(ip) as conn:
        for ch in text:
            if ch in SHIFTED:
                key = f"shift-{SHIFTED[ch]}"
            elif ch.isupper():
                key = f"shift-{ch.lower()}"
            elif ch in TEXT_KEYS:
                key = TEXT_KEYS[ch]
            else:
                key = ch
            out = conn.hmp(f"sendkey {key}")
            if out:
                raise SystemExit(f"sendkey {key!r}: {out}")
            time.sleep(delay)


def click(slug: str, x: int, y: int, width: int = 0, height: int = 0) -> None:
    """Click at absolute guest coordinates.

    QEMU's absolute pointer axis is a fixed 0-32767 range regardless of the
    guest resolution, so the caller's pixel coordinates have to be scaled by the
    CURRENT framebuffer size — which is why width/height default to reading it.

    KNOWN LIMITATION: this moves the pointer but has not been made to land
    accurately. Selecting the absolute device first (below) was necessary but
    not sufficient — clicks still miss, and locating the drawn cursor to
    calibrate is unreliable because a diff of two framebuffers picks up the old
    cursor being erased as well as the new one being drawn. Keyboard driving
    (`keys`/`type`) is the dependable path today; anything that genuinely needs
    a pointer is faster to do in the browser console.
    """
    ip = _ip(slug)
    if not width or not height:
        _, width, height = screenshot.capture_raw(ip)
    ax = int(x * 32767 / max(1, width - 1))
    ay = int(y * 32767 / max(1, height - 1))
    with qmp.Qmp(ip) as conn:
        # Absolute events go to whichever mouse QEMU has ACTIVE, and that is the
        # relative PS/2 device by default even when a USB tablet is present —
        # so the pointer simply never moves and the click lands nowhere. Switch
        # to the absolute device first. `info mice` marks the active one with
        # a leading '*'.
        for line in conn.hmp("info mice").splitlines():
            if "absolute" in line.lower() and not line.strip().startswith("*"):
                index = line.split("#")[1].split(":")[0].strip()
                conn.hmp(f"mouse_set {index}")
                break
        conn.execute(
            "input-send-event",
            events=[
                {"type": "abs", "data": {"axis": "x", "value": ax}},
                {"type": "abs", "data": {"axis": "y", "value": ay}},
            ],
        )
        for down in (True, False):
            conn.execute(
                "input-send-event",
                events=[{"type": "btn", "data": {"down": down, "button": "left"}}],
            )
            time.sleep(0.05)


def _fingerprint(ip: str) -> tuple[str, int, int]:
    rgb, w, h = screenshot.capture_raw(ip)
    return hashlib.sha1(bytes(rgb)).hexdigest(), w, h


def wait_stable(slug: str, stable: int = 3, timeout: int = 900, interval: float = 5.0) -> str:
    """Block until the screen has been unchanged for `stable` consecutive polls.

    An installer gives no completion signal that is visible from outside the
    guest, so "the picture stopped changing" is the only usable one. It is a
    heuristic and says so: a long silent file copy with a static screen looks
    identical to being finished, which is why callers still check the shot.
    """
    ip = _ip(slug)
    deadline = time.time() + timeout
    last, count = None, 0
    while time.time() < deadline:
        try:
            digest, w, h = _fingerprint(ip)
        except screenshot.ScreenshotError:
            time.sleep(interval)
            continue
        if digest == last:
            count += 1
            if count >= stable:
                return f"stable {w}x{h} after {int(time.time() - (deadline - timeout))}s"
        else:
            last, count = digest, 0
        time.sleep(interval)
    return "timed out waiting for the screen to settle"


def shot(slug: str, path: str) -> str:
    rgb, w, h = screenshot.capture_raw(_ip(slug))
    with open(path, "wb") as fh:
        fh.write(screenshot.thumbnail(rgb, w, h, max_width=min(w, 900)))
    return f"{w}x{h} -> {path}"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    k8s.init()
    cmd, slug, rest = argv[1], argv[2], argv[3:]
    if cmd == "shot":
        print(shot(slug, rest[0] if rest else "/tmp/shot.png"))
    elif cmd == "keys":
        keys(slug, rest)
        print(f"sent {len(rest)} key(s)")
    elif cmd == "type":
        type_text(slug, rest[0])
        print(f"typed {len(rest[0])} char(s)")
    elif cmd == "click":
        click(slug, int(rest[0]), int(rest[1]))
        print(f"clicked {rest[0]},{rest[1]}")
    elif cmd == "wait":
        print(wait_stable(slug, *(int(a) for a in rest)))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
