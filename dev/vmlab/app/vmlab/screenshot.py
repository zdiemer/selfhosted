"""Grab what a guest is showing, as a PNG.

QEMU can write a PNG itself (`screendump ... format=png`), but only to a path
inside the session pod — which the UI would then have to reach through a shared
volume, putting guest-adjacent storage back into a design that deliberately has
none. Pulling the framebuffer over VNC instead keeps every byte in the UI pod
and adds no mount, no sidecar and no shared writable path.

The RFB client here is the same one used to verify these guests were genuinely
booting rather than sitting on a blank screen, so its behaviour against this
exact image is known rather than assumed.

PNG is encoded by hand because the alternative is Pillow — a compiled
dependency, in an image whose requirements.txt is deliberately wheels-only, for
something zlib already does.
"""

from __future__ import annotations

import logging
import socket
import struct
import zlib

logger = logging.getLogger(__name__)

VNC_PORT = 5900
_TIMEOUT = 20.0
# A framebuffer is width*height*4 bytes; 1920x1200 is ~9MB. The cap is a guard
# against a malformed rect header asking us to allocate the world.
_MAX_PIXELS = 4096 * 4096


class ScreenshotError(RuntimeError):
    pass


def _recvn(sock: socket.socket, n: int) -> bytes:
    parts = []
    remaining = n
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise ScreenshotError(f"connection closed with {remaining} bytes outstanding")
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _png(width: int, height: int, rgb: bytearray) -> bytes:
    """Minimal truecolour PNG. Filter type 0 on every scanline."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


# The UI renders these at 148px wide. A full 1280x720 capture is ~1.3MB of PNG,
# and a wall of twelve would be ~16MB on a page that polls — so a thumbnail is
# stored alongside the original and served by default.
THUMB_WIDTH = 480


def capture(host: str, port: int = VNC_PORT, timeout: float = _TIMEOUT) -> tuple[bytes, int, int]:
    """Return (png_bytes, width, height) for the guest's current screen."""
    rgb, w, h = capture_raw(host, port, timeout)
    return _png(w, h, rgb), w, h


def thumbnail(rgb: bytearray, width: int, height: int, max_width: int = THUMB_WIDTH) -> bytes:
    """Box-average down to max_width. Averaging rather than nearest-neighbour
    because text-mode screens are mostly thin strokes, which point-sampling
    drops entirely — a BIOS screen would thumbnail to solid black."""
    if width <= max_width:
        return _png(width, height, rgb)
    scale = width / max_width
    tw = max_width
    th = max(1, int(height / scale))
    out = bytearray(tw * th * 3)
    for ty in range(th):
        y0, y1 = int(ty * scale), max(int(ty * scale) + 1, int((ty + 1) * scale))
        y1 = min(y1, height)
        for tx in range(tw):
            x0, x1 = int(tx * scale), max(int(tx * scale) + 1, int((tx + 1) * scale))
            x1 = min(x1, width)
            r = g = b = n = 0
            for y in range(y0, y1):
                base = y * width * 3
                for x in range(x0, x1):
                    i = base + x * 3
                    r += rgb[i]; g += rgb[i + 1]; b += rgb[i + 2]
                    n += 1
            o = (ty * tw + tx) * 3
            out[o] = r // n; out[o + 1] = g // n; out[o + 2] = b // n
    return _png(tw, th, out)


def capture_raw(host: str, port: int = VNC_PORT, timeout: float = _TIMEOUT) -> tuple[bytearray, int, int]:
    """Return (rgb_bytes, width, height) for the guest's current screen."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ScreenshotError(f"cannot reach the guest display: {exc}") from exc
    sock.settimeout(timeout)
    try:
        _recvn(sock, 12)
        sock.sendall(b"RFB 003.008\n")

        types = _recvn(sock, _recvn(sock, 1)[0])
        if not types:
            raise ScreenshotError("server offered no security types")
        # 1 = None. These consoles are reachable only from this pod, enforced by
        # NetworkPolicy, so QEMU is left without a VNC password on purpose.
        sock.sendall(bytes([1 if 1 in types else types[0]]))
        if struct.unpack(">I", _recvn(sock, 4))[0] != 0:
            raise ScreenshotError("display refused the connection")

        # Shared flag: do NOT disconnect anyone already watching the console.
        sock.sendall(b"\x01")
        width, height = struct.unpack(">HH", _recvn(sock, 4))
        pf = _recvn(sock, 16)
        _recvn(sock, struct.unpack(">I", _recvn(sock, 4))[0])  # desktop name

        bpp, _depth, big_endian, true_colour = pf[0], pf[1], pf[2], pf[3]
        rmax, gmax, bmax = struct.unpack(">HHH", pf[4:10])
        rshift, gshift, bshift = pf[10], pf[11], pf[12]
        if not true_colour:
            raise ScreenshotError("palette displays are not supported")
        if bpp not in (8, 16, 32):
            raise ScreenshotError(f"unsupported bits-per-pixel {bpp}")
        if width * height > _MAX_PIXELS:
            raise ScreenshotError(f"framebuffer too large: {width}x{height}")

        # Raw only. Asking for Raw is what keeps this readable — the guest is
        # idle at a save point, so the bandwidth saved by a real encoding buys
        # nothing and costs a decoder for each one.
        sock.sendall(struct.pack(">BBHi", 2, 0, 1, 0))
        sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))

        rgb = bytearray(width * height * 3)
        got_any = False
        while True:
            msg = _recvn(sock, 1)[0]
            if msg != 0:
                # ServerCutText / Bell / SetColourMap arrive unbidden; drain and
                # keep waiting for the update rather than failing.
                if msg == 3:
                    _recvn(sock, 7)
                elif msg == 1:
                    _recvn(sock, 3)
                    _recvn(sock, struct.unpack(">I", _recvn(sock, 4))[0])
                continue
            _recvn(sock, 1)
            nrects = struct.unpack(">H", _recvn(sock, 2))[0]
            for _ in range(nrects):
                rx, ry, rw, rh = struct.unpack(">HHHH", _recvn(sock, 8))
                enc = struct.unpack(">i", _recvn(sock, 4))[0]
                if enc != 0:
                    raise ScreenshotError(f"unexpected encoding {enc}")
                data = _recvn(sock, rw * rh * (bpp // 8))
                _blit(data, rgb, width, rx, ry, rw, rh, bpp, big_endian,
                      rmax, gmax, bmax, rshift, gshift, bshift)
                got_any = True
            if got_any:
                break

        return rgb, width, height
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _blit(data, rgb, fb_width, rx, ry, rw, rh, bpp, big_endian,
          rmax, gmax, bmax, rshift, gshift, bshift) -> None:
    step = bpp // 8
    order = "big" if big_endian else "little"
    for row in range(rh):
        src = row * rw * step
        dst = ((ry + row) * fb_width + rx) * 3
        for col in range(rw):
            px = int.from_bytes(data[src:src + step], order)
            src += step
            # Scale each channel off its own max rather than assuming 8 bits:
            # 16bpp guests (which some of these old OSes use during install)
            # have 5/6/5 channels.
            rgb[dst] = ((px >> rshift) & rmax) * 255 // rmax
            rgb[dst + 1] = ((px >> gshift) & gmax) * 255 // gmax
            rgb[dst + 2] = ((px >> bshift) & bmax) * 255 // bmax
            dst += 3
