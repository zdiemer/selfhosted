#!/usr/bin/env bash
(
set -euo pipefail
[[ "${STEAM_INPUT_POLICY:-}" == virtual-input-v1 ]] || { echo 'Required NRI input policy is absent'; exit 1; }
# Devices are allocated by kubelet. Never manufacture host devices here.
for device in /dev/dri/card1 /dev/dri/renderD128 /dev/uinput /dev/tty0 /dev/tty7; do
    [[ -c "$device" ]] || { echo "Required allocated device missing: $device"; exit 1; }
done
chgrp 1000 /dev/uinput
chmod 0660 /dev/uinput
# No udev daemon or sysfs writes. A separate, minimally capable container
# maintains the read-only input directory using virtual devices only.
cat > /etc/cont-init.d/30-configure_udev.sh <<'EOF'
sed -i 's/^autostart=.*/autostart=false/' /etc/supervisor.d/udev.ini
EOF
# Exec Xorg directly so supervisor owns the server, including restart signals.
cat > /usr/bin/start-xorg.sh <<'EOF'
#!/usr/bin/env bash
exec /usr/bin/Xorg -ac -noreset -novtswitch -sharevts -nolisten tcp -iglx -dpms -s off vt7 "${DISPLAY:?}"
EOF
cat > /etc/supervisor.d/virtual-inputs.ini <<'EOF'
[program:virtual-inputs]
command=python3 /opt/chart-init/xorg-inputs.py
user=root
autostart=true
autorestart=true
stdout_logfile=/var/log/supervisor/virtual-inputs.log
stderr_logfile=/var/log/supervisor/virtual-inputs.err.log
EOF
# Upstream Flatpak init remounts /proc, which requires SYS_ADMIN. Steam's native
# client and games do not need that host-level initialization.
printf '%s\n' 'echo "Flatpak host /proc setup disabled"' > /etc/cont-init.d/80-configure_flatpak.sh
)
