"""Seed emulator firmware/configuration from the read-only library, idempotently."""
from pathlib import Path
import configparser
import os
import shutil
import zipfile

root = Path('/config')
bios = Path('/romm/library/_bios')

def link(source, dest):
    if not source.exists() or dest.exists() or dest.is_symlink():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source)

def ini(path, settings):
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.optionxform = str
    if path.exists(): cfg.read(path)
    for section, values in settings.items():
        if not cfg.has_section(section): cfg.add_section(section)
        for key, value in values.items():
            # Preserve settings chosen later through the emulator desktop.
            if not cfg.has_option(section, key): cfg.set(section, key, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as f: cfg.write(f, space_around_delimiters=True)

# Standard RetroArch BIOS filenames remain read-only. Keep directories such as
# blueMSX's Machines/Databases intact; writable save trees live elsewhere.
system = root / '.config/retroarch/system'
system.mkdir(parents=True, exist_ok=True)
for platform in sorted(bios.iterdir()):
    if not platform.is_dir(): continue
    for src in sorted(platform.iterdir()):
        if src.name.endswith(('.qcow2', '.PUP', '.keys', '.zip')) or ' (copy)' in src.name: continue
        link(src, system / src.name)
ini(root / '.config/PCSX2/inis/PCSX2.ini', {
    'UI': {'SettingsVersion': '1', 'SetupWizardIncomplete': 'false'},
    'Folders': {'Bios': str(bios / 'PS2')},
    'Filenames': {'BIOS': 'ps2-0230a-20080220.bin'},
    # The pinned image lacks libshaderc.so.1 required by PCSX2's Vulkan path.
    # OpenGL (12) still uses the Radeon GPU and avoids that missing dependency.
    'EmuCore/GS': {'Renderer': '12'},
})
# The PPA build in this image omits the official game-patch bundle. Keep the
# installed upstream bundle on the PVC and link it back after each rebuild.
link(root / '.config/PCSX2/resources/patches.zip',
     Path('/usr/share/PCSX2/resources/patches.zip'))
ini(root / '.local/share/duckstation/settings.ini', {
    'BIOS': {'SearchDirectory': str(bios / 'PSX'), 'PathNTSCU': 'scph5501.bin',
             'PathNTSCJ': 'scph5500.bin', 'PathPAL': 'scph5502.bin'},
})
for name in ('dc_boot.bin', 'dc_flash.bin'):
    link(bios / 'Dreamcast' / name, root / '.local/share/flycast/data' / name)
    link(bios / 'Dreamcast' / name, system / 'dc' / name)
link(bios / 'Nintendo 3DS/seeddb.bin', root / '.local/share/azahar-emu/sysdata/seeddb.bin')
for name in ('prod.keys', 'title.keys'):
    link(bios / 'Switch' / name, root / '.local/share/eden/keys' / name)
firmware = bios / 'Switch/switch-firmware.zip'
registered = root / '.local/share/eden/nand/system/Contents/registered'
if firmware.exists():
    registered.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(firmware) as z:
        for item in z.infolist():
            name = Path(item.filename).name
            if not name.endswith('.nca') or item.is_dir(): continue
            dest = registered / name
            if not dest.exists():
                with z.open(item) as src, dest.open('wb') as out: shutil.copyfileobj(src, out)

# xemu writes saves to its HDD; never point it at the NAS's read-only template.
hdd = root / 'xemu/xbox_hdd.qcow2'
if not hdd.exists() and (bios / 'Xbox/xbox_hdd.qcow2').exists():
    hdd.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bios / 'Xbox/xbox_hdd.qcow2', hdd)
xemu = root / '.local/share/xemu/xemu/xemu.toml'
if not xemu.exists():
    xemu.parent.mkdir(parents=True, exist_ok=True)
    xemu.write_text('[sys.files]\n' +
        f'bootrom_path = "{bios}/Xbox/mcpx_1.0.bin"\n' +
        f'flashrom_path = "{bios}/Xbox/Complex_4627v1.03.bin"\n' +
        f'hdd_path = "{hdd}"\n')

# The custom-init hook runs as root; emulator processes use abc (1000:1000).
# Do not follow firmware symlinks back into the read-only NAS.
for directory, dirs, files in os.walk(root):
    os.chown(directory, 1000, 1000)
    for name in dirs + files:
        os.chown(os.path.join(directory, name), 1000, 1000, follow_symlinks=False)
print('Firmware and emulator defaults prepared; existing settings preserved.')
