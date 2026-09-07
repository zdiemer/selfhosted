#!/bin/sh
# Bring up one 86Box session. Everything here comes from env set by spec.py;
# nothing is taken from a request.
set -eu

WORK=${BOX_WORK:-/86box}
MACHINE=${BOX_MACHINE:-p2bls}
CPU_FAMILY=${BOX_CPU_FAMILY:-pentium2_deschutes}
CPU_SPEED=${BOX_CPU_SPEED:-350000000}
MEM_KB=${BOX_MEM_KB:-65536}
GFXCARD=${BOX_GFXCARD:-virge375_pci}
SNDCARD=${BOX_SNDCARD:-sb16}
FDD=${BOX_FDD_TYPE:-35_2hd}
DISK_MB=${BOX_DISK_MB:-2048}
ISO=${BOX_ISO:-}
SCREEN=${BOX_SCREEN:-1024x768x24}
VNC_PORT=${BOX_VNC_PORT:-5900}
WEB_PORT=${BOX_WEB_PORT:-8006}

mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

# An empty NVR is how a machine that has never been switched on looks, and
# it is what makes the BIOS halt at "Press F1" below.
FRESH_NVR=0
[ -d "$WORK/nvr" ] && [ -n "$(ls -A "$WORK/nvr" 2>/dev/null)" ] || FRESH_NVR=1
mkdir -p "$WORK/nvr"

# The hard disk, made once and then left alone. 86Box takes a flat image plus
# its geometry, and it does NOT derive one from the other: the parameters below
# are what the guest's BIOS reports, so they have to describe the file exactly
# or the partition table and the disk disagree.
#
# 63 sectors x 16 heads is the standard LBA-ish translation these BIOSes use,
# which makes one cylinder 63*16*512 = 516096 bytes.
DISK="$WORK/disk.img"
CYLS=$(( DISK_MB * 1024 * 1024 / 516096 ))
[ "$CYLS" -lt 1 ] && CYLS=1
[ "$CYLS" -gt 16383 ] && CYLS=16383
if [ ! -f "$DISK" ]; then
  echo "==> creating a ${DISK_MB}MB disk (${CYLS} cylinders, 16 heads, 63 sectors)"
  # Sparse: the file reads as the full size but occupies only what is written,
  # which matters on a PVC sized for the disk rather than for its zeroes.
  truncate -s $(( CYLS * 516096 )) "$DISK"
else
  echo "==> reusing the disk in $WORK"
fi

# 86Box rewrites this file itself whenever settings change in its GUI, so the
# fields the pod owns are restated on every launch. Anything the guest changed
# about its own machine would not survive the pod in any case.
#
# Only the keys that decide what the machine IS are set. 86Box fills in every
# other default on load, and listing them here would mean maintaining a copy of
# its defaults that silently rots against the emulator.
{
  echo "[General]"
  echo "video_fullscreen_scale = 1"
  # Confirmed by reading back what 86Box wrote: its default resolves to the
  # software renderer here, which is what is wanted — there is no GPU behind
  # Xvfb, and the Vulkan path logs "No Vulkan library available" and falls back.
  echo "vid_renderer = qt_software"
  echo "confirm_exit = 0"
  echo "window_remember = 0"
  echo
  echo "[Machine]"
  echo "machine = ${MACHINE}"
  echo "cpu_family = ${CPU_FAMILY}"
  echo "cpu_speed = ${CPU_SPEED}"
  echo "cpu_use_dynarec = 1"
  echo "mem_size = ${MEM_KB}"
  echo "time_sync = local"
  echo
  echo "[Video]"
  echo "gfxcard = ${GFXCARD}"
  echo
  echo "[Input devices]"
  # PS/2 rather than serial: it is what these boards have, and it is what
  # x11vnc's absolute pointer maps onto most predictably.
  echo "mouse_type = ps2"
  echo
  echo "[Sound]"
  echo "sndcard = ${SNDCARD}"
  echo "sound_muted = 1"
  echo
  echo "[Hard disks]"
  echo "hdd_01_parameters = 63, 16, ${CYLS}, 0, ide"
  echo "hdd_01_fn = disk.img"
  echo "hdd_01_ide_channel = 0:0"
  echo
  echo "[Floppy and CD-ROM drives]"
  echo "fdd_01_type = ${FDD}"
  # "atapi", not "ide". hdd_string_to_bus() accepts both, but a CD-ROM given
  # "ide" is not attached as one: 86Box quietly deletes the channel key and the
  # BIOS reports "Sec. Master: None", which looks exactly like a missing image.
  # The first field is the audio-enable flag, not the drive number.
  #
  # Secondary master, leaving the primary channel to the hard disk. Period
  # machines expect the CD there and their setup programs look for it there.
  echo "cdrom_01_parameters = 1, atapi"
  echo "cdrom_01_ide_channel = 1:0"
  echo "cdrom_01_speed = 8"
  if [ -n "$ISO" ]; then
    echo "cdrom_01_image_path = ${ISO}"
  fi
} > "$WORK/86box.cfg"

term() {
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

# -forever and -shared because vmlab opens a second client of its own for the
# live tiles, and a screenshot must not disconnect whoever is watching.
echo "==> x11vnc on :$VNC_PORT"
x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -forever -shared -nopw \
       -noxdamage -quiet -bg -o /tmp/x11vnc.log

echo "==> noVNC on :$WEB_PORT"
websockify --web=/usr/share/novnc "$WEB_PORT" "localhost:$VNC_PORT" &

echo "==> 86Box: ${MACHINE}, ${CPU_FAMILY} @ ${CPU_SPEED}Hz, $(( MEM_KB / 1024 ))MB, ${GFXCARD}"
[ -n "$ISO" ] && echo "==> CD: $ISO"
# --rompath because the ROM set stays in the image; --vmpath is the writable
# volume holding the config, the disk and the NVR (each machine's CMOS).
86Box --rompath /usr/share/86box/roms --vmpath "$WORK" --fullscreen &
BOX_PID=$!

if [ "$FRESH_NVR" = "1" ]; then
  # Timed rather than triggered, because the only signal available from out here
  # is the picture, and reading "Press F1" off a framebuffer to decide whether to
  # press F1 is more machinery than the problem deserves. If the guess is early
  # or late the key lands somewhere harmless and the console still works — this
  # removes a papercut, it is not load-bearing.
  ( sleep 25
    python3 /usr/local/bin/first-boot-keys.py 127.0.0.1 "$VNC_PORT" || true ) &
fi

wait "$BOX_PID"
echo "==> 86Box exited"
