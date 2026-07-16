#!/usr/bin/env bash
# Serve a Headlamp login token over HTTP, so you can grab it from a phone or
# another machine without a terminal.
#
# WHAT THIS HANDS OUT: Headlamp's ServiceAccount is bound to cluster-admin, so
# the token this serves is full control of the cluster, over plaintext HTTP, with
# no authentication in front of it. Anyone who can reach this host on this port
# while it is running gets it.
#
# Ported from talaria's scripts/headlamp/token-server.sh with two changes, both
# because of the sentence above:
#
#   1. The URL now carries a random path. The original served at / on a fixed
#      port, so anything sweeping the LAN for open ports would be handed a
#      cluster-admin token by simply connecting.
#   2. It exits after serving the token once (or after --timeout seconds). The
#      original ran until Ctrl-C, so the window stayed open as long as you forgot
#      about the terminal.
#
# Same workflow: run it, open the URL it prints, copy the token.
#
#   ./token-server.sh                 # one token, then exit
#   ./token-server.sh --timeout 600   # wait longer before giving up
#   ./token-server.sh --serve-forever # old behaviour, if you really want it

set -euo pipefail

PORT="${PORT:-8888}"
TIMEOUT=300
FOREVER=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --serve-forever) FOREVER=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

command -v kubectl >/dev/null || { echo "kubectl required"; exit 1; }

# Fail before opening a port if we can't mint a token at all.
kubectl create token headlamp -n headlamp >/dev/null 2>&1 \
  || { echo "cannot mint a token — is the headlamp release installed?"; exit 1; }

# -u: the URL must appear immediately even when this is piped or redirected —
# a buffered banner would leave you staring at an empty screen.
PORT="$PORT" TIMEOUT="$TIMEOUT" FOREVER="$FOREVER" python3 -u - <<'EOF'
import http.server
import os
import secrets
import socket
import subprocess
import sys

PORT = int(os.environ["PORT"])
TIMEOUT = float(os.environ["TIMEOUT"])
FOREVER = os.environ["FOREVER"] == "1"

# An unguessable path, so a port sweep finds a 404 rather than a cluster-admin
# token. This is not real authentication — it just means the URL has to be given
# to you rather than stumbled upon.
PATH = "/" + secrets.token_urlsafe(9)
served = False


def local_ip():
    # The address a LAN client would actually reach us on, rather than whatever
    # hostname resolves to (often 127.0.1.1).
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return socket.gethostname()
    finally:
        s.close()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global served
        if self.path != PATH:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found\n")
            return
        try:
            token = subprocess.check_output(
                ["kubectl", "create", "token", "headlamp", "-n", "headlamp"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except subprocess.CalledProcessError:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"failed to generate token\n")
            return
        body = (token + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        # Don't let a phone browser keep this in history/cache any longer than
        # it must.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        served = True

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.address_string(), fmt % args), flush=True)


# Bind before advertising: printing the URL first means a failed bind (port
# already busy) still hands you a URL that was never going to work.
try:
    server = http.server.HTTPServer(("", PORT), Handler)
except OSError as e:
    print("cannot listen on port %d: %s" % (PORT, e))
    print("(another token-server still running? try --port)")
    sys.exit(1)

print("=== Headlamp token server ===")
print("Open:  http://%s:%d%s" % (local_ip(), PORT, PATH))
print("")
print("This serves a CLUSTER-ADMIN token over plaintext HTTP.")
print("Exits after serving it once%s." % ("" if not FOREVER else " — disabled by --serve-forever"))
print("")

with server:
    server.timeout = TIMEOUT
    try:
        while True:
            server.handle_request()
            if served and not FOREVER:
                print("=== Token served — shutting down ===")
                sys.exit(0)
    except KeyboardInterrupt:
        print("\n=== Stopped ===")
        sys.exit(0)
EOF
