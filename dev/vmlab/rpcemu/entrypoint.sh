#!/bin/sh
# Bring up one RISC OS session: an X server nobody can see, RPCEmu on it, and
# noVNC in front. Everything here is driven by env set by spec.py; nothing is
# taken from a request.
set -eu

WORK=${RPCEMU_WORK:-/rpcemu}
MEM_SIZE=${RPCEMU_MEM_SIZE:-128}
VRAM_SIZE=${RPCEMU_VRAM_SIZE:-8}
MODEL=${RPCEMU_MODEL:-RPCSA}
SCREEN=${RPCEMU_SCREEN:-1280x1024x16}
VNC_PORT=${RPCEMU_VNC_PORT:-5900}
WEB_PORT=${RPCEMU_WEB_PORT:-8006}

mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
mkdir -p "$WORK"

# The template is read-only and lives in the image; the working tree is either
# an emptyDir that dies with the pod or a PVC that does not. Seeded only when
# empty, so a persistent RISC OS keeps whatever it has been doing — the copy
# would otherwise overwrite !Boot on every launch, silently undoing the guest's
# own configuration.
FRESH=0
if [ ! -e "$WORK/roms/riscos" ]; then
  echo "==> seeding a fresh RISC OS in $WORK"
  # -r, not -a: preserving ownership and timestamps onto a volume this user
  # does not own fails, and there is nothing in the mode bits worth keeping.
  cp -r /template/. "$WORK/"
  FRESH=1
else
  echo "==> resuming the RISC OS in $WORK"
fi

# rpc.cfg is rewritten every launch. RPCEmu also writes this file itself when
# the user changes settings in its GUI, so the fields the pod owns are restated
# rather than merged: memory and model come from the config, and anything the
# guest changed about them would not survive the pod anyway.
#
# network_type=off is not a default being accepted. RPCEmu's other modes want a
# tap device and NET_ADMIN, which is exactly the capability this lab refuses to
# hold; NetworkPolicy is the backstop, but not asking is the actual control.
cat > "$WORK/rpc.cfg" <<CFG
[General]
cdrom_enabled=0
cdrom_type=0
cpu_idle=1
mem_size=${MEM_SIZE}
vram_size=${VRAM_SIZE}
model=${MODEL}
mouse_following=1
mouse_twobutton=0
network_type=off
refresh_rate=60
show_fullscreen_message=0
sound_enabled=0
stretch_mode=1
CFG

term() {
  # Killing the process group rather than each pid: RPCEmu spawns Qt threads,
  # and a session that is stopped must actually stop or it keeps a quota slot.
  trap - TERM INT
  kill 0 2>/dev/null || true
}
trap term TERM INT

echo "==> Xvfb on $DISPLAY at $SCREEN"
Xvfb "$DISPLAY" -screen 0 "$SCREEN" -nolisten tcp -noreset >/tmp/xvfb.log 2>&1 &
for _ in $(seq 1 50); do
  xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
  sleep 0.2
done
xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || {
  echo "ERROR: no X display; Xvfb said:"; cat /tmp/xvfb.log; exit 1; }

# x11vnc before the emulator, because the first boot needs to type into it.
# -forever and -shared because vmlab connects a second client of its own for the
# live tiles and the save-point pictures, and a screenshot must not disconnect
# whoever is watching.
echo "==> x11vnc on :$VNC_PORT"
x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -forever -shared -nopw \
       -noxdamage -quiet -bg -o /tmp/x11vnc.log

echo "==> noVNC on :$WEB_PORT"
websockify --web=/usr/share/novnc "$WEB_PORT" "localhost:$VNC_PORT" &

cd "$WORK"

# A Risc PC keeps its boot filesystem in battery-backed CMOS, and the one RPCEmu
# ships is set to ADFS - a hard disc that does not exist here - so a fresh
# machine stops at the supervisor `*` prompt instead of reaching the desktop.
# The manual's answer is two commands typed at that prompt, and typing them is
# what this does, once, on the boot that follows a fresh seed:
#
#   *configure filesystem hostfs
#   *configure boot
#
# It costs about a minute, and only ever that once: RPCEmu writes cmos.ram the
# instant RISC OS updates the CMOS checksum, and on a persist:true config that
# file outlives the pod. The emulator is then restarted, because a Risc PC reads
# its CMOS at reset and nowhere else.
if [ "$FRESH" = "1" ]; then
  echo "==> first boot: configuring the CMOS to boot from HostFS"
  rpcemu &
  SETUP_PID=$!
  sleep 45
  python3 /usr/local/bin/configure-cmos.py 127.0.0.1 "$VNC_PORT" || \
    echo "WARNING: could not configure the CMOS; expect a supervisor prompt"
  sleep 5
  kill "$SETUP_PID" 2>/dev/null || true
  wait "$SETUP_PID" 2>/dev/null || true
  echo "==> restarting into the configured machine"
fi

echo "==> RPCEmu ($MODEL, ${MEM_SIZE}MB, ${VRAM_SIZE}MB VRAM)"
rpcemu &
RPCEMU_PID=$!

# The emulator is the session. When RISC OS exits, so does the pod, which is
# what makes activeDeadlineSeconds and the idle reaper the only two ways a
# session ends rather than three.
wait "$RPCEMU_PID"
echo "==> RPCEmu exited"
