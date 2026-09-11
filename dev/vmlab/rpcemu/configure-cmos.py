"""Type RISC OS's two configuration commands into a running RPCEmu, over RFB.

Used once, at image build time, to turn the CMOS RPCEmu ships — which boots
ADFS, a disc that does not exist here — into one that boots HostFS. See the
Dockerfile's cmos stage for why this is done at build time rather than shipped
as a binary blob or re-typed into every session.

RFB rather than xdotool, and that is not a preference. xdotool's XTEST events
went nowhere: with no window manager there is no focused window for them to go
to. x11vnc delivers a pointer move first, which is what gives the emulator
focus, and then the keys follow it. That was established by driving a live
session this way, not by reasoning about it.
"""

import socket
import struct
import sys
import time

CONFIGURE = ("configure filesystem hostfs", "configure boot")
RETURN = 0xFF0D


def connect(host, port):
    sock = socket.create_connection((host, port), timeout=30)
    sock.settimeout(30)
    sock.recv(12)
    sock.sendall(b"RFB 003.008\n")
    types = sock.recv(sock.recv(1)[0])
    sock.sendall(bytes([1 if 1 in types else types[0]]))
    if struct.unpack(">I", sock.recv(4))[0] != 0:
        raise SystemExit("display refused the connection")
    sock.sendall(b"\x01")  # shared
    width, height = struct.unpack(">HH", sock.recv(4))
    sock.recv(16)
    sock.recv(struct.unpack(">I", sock.recv(4))[0])
    return sock, width, height


def main():
    host, port = sys.argv[1], int(sys.argv[2])
    sock, width, height = connect(host, port)
    print(f"configure-cmos: connected, {width}x{height}", flush=True)

    # Focus follows the pointer when nothing else claims it.
    sock.sendall(struct.pack(">BBHH", 5, 0, 400, 400))
    time.sleep(0.5)

    for command in CONFIGURE:
        print(f"configure-cmos: {command}", flush=True)
        for char in command:
            for down in (1, 0):
                sock.sendall(struct.pack(">BBHI", 4, down, 0, ord(char)))
                time.sleep(0.03)
            time.sleep(0.05)
        for down in (1, 0):
            sock.sendall(struct.pack(">BBHI", 4, down, 0, RETURN))
            time.sleep(0.05)
        # RISC OS writes the CMOS checksum after each *configure, and RPCEmu
        # saves the file the moment it sees that write.
        time.sleep(4)


if __name__ == "__main__":
    main()
