"""Press F1 once, at the POST of a machine that has never been switched on.

A real PC with a blank CMOS says "CMOS checksum error - Defaults loaded" and
stops at "Press F1 to continue", and 86Box is faithful about it. That is correct
behaviour and it only happens once — the NVR is written on the first successful
boot and persists with the volume — but "once" is every fresh session, and a
guest that halts before it reaches the CD it was given is not a guest anyone
wants to be handed.

So the keypress is sent exactly once, on the boot that follows a fresh NVR, and
never afterwards. Over RFB rather than xdotool for the reason the RPCEmu engine
documents: with no window manager there is no focused window for XTEST events
to reach, and moving the pointer first is what gives the emulator focus.
"""

import socket
import struct
import sys
import time

F1 = 0xFFBE


def main() -> int:
    host, port = sys.argv[1], int(sys.argv[2])
    try:
        sock = socket.create_connection((host, port), timeout=30)
    except OSError as exc:
        print(f"first-boot-keys: cannot reach the display: {exc}", flush=True)
        return 1
    sock.settimeout(30)
    sock.recv(12)
    sock.sendall(b"RFB 003.008\n")
    types = sock.recv(sock.recv(1)[0])
    sock.sendall(bytes([1 if 1 in types else types[0]]))
    if struct.unpack(">I", sock.recv(4))[0] != 0:
        print("first-boot-keys: display refused the connection", flush=True)
        return 1
    sock.sendall(b"\x01")  # shared: do not evict whoever is watching
    sock.recv(4)
    sock.recv(16)
    sock.recv(struct.unpack(">I", sock.recv(4))[0])

    sock.sendall(struct.pack(">BBHH", 5, 0, 400, 300))
    time.sleep(0.5)
    for down in (1, 0):
        sock.sendall(struct.pack(">BBHI", 4, down, 0, F1))
        time.sleep(0.1)
    print("first-boot-keys: sent F1 to clear the CMOS-defaults halt", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
