"""Configure only Sunshine virtual desktop inputs, without host udev access."""
from pathlib import Path
import subprocess
import time

config = Path('/etc/X11/xorg.conf.d/90-sunshine-inputs.conf')
previous = None
while True:
    inputs = []
    for entry in sorted(Path('/sys/class/input').glob('event*')):
        if '/devices/virtual/input/' not in str(entry.resolve()):
            continue
        try:
            name = (entry / 'device/name').read_text().strip()
        except OSError:
            continue
        if name not in ('Keyboard passthrough', 'Mouse passthrough', 'Mouse passthrough (absolute)'):
            continue
        if not Path('/dev/input', entry.name).exists():
            continue
        inputs.append((entry.name, name))
    if len(inputs) == 3 and inputs != previous:
        sections = ['Section "ServerFlags"\n Option "AutoAddDevices" "false"\nEndSection\n']
        for event, name in inputs:
            sections.append(f'''Section "InputDevice"
 Identifier "{name}"
 Driver "evdev"
 Option "Device" "/dev/input/{event}"
 Option "AlwaysCore" "true"
EndSection
''')
        sections.append('Section "ServerLayout"\n Identifier "Sunshine"\n' + ''.join(f' InputDevice "{name}" "{"CoreKeyboard" if name == "Keyboard passthrough" else "CorePointer" if name == "Mouse passthrough" else "SendCoreEvents"}"\n' for _, name in inputs) + 'EndSection\n')
        text = '\n'.join(sections)
        if not config.exists() or config.read_text() != text:
            config.write_text(text)
            subprocess.run(['supervisorctl', 'restart', 'xorg'], check=True)
        previous = inputs
        print('Configured Sunshine keyboard and mouse', flush=True)
    time.sleep(1)
