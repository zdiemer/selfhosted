"""Exercise the rendered builder against an isolated NAS-shaped fixture."""
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

CHART = Path(__file__).resolve().parents[1]

class LibraryBuilderTest(unittest.TestCase):
    def test_streaming_layout_preserves_firmware_and_omits_auxiliary_content(self):
        args = ['helm', 'template', 'romm', str(CHART)]
        for key in ['romm.authSecretKey', 'db.password', 'db.rootPassword',
                    'smb.username', 'smb.password', 'streaming.brokerSecret']:
            args += ['--set', f'{key}=test-only']
        objects = list(yaml.safe_load_all(subprocess.check_output(args)))
        maps = {o['metadata']['name']: o for o in objects if o['kind'] == 'ConfigMap'}
        source = maps['romm-library-builder']['data']['build.py']
        config = maps['romm-config']['data']['config.yml']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / 'library'
            nas = library / '.nas/Roms'
            for file in ['GameCube/game.rvz', 'PS2/game.bin', 'PS2/game.cue',
                         'PS2/_bios/bios.bin', 'Xbox/game.iso', 'Xbox/game.iso.gz',
                         'Nintendo 3DS/_decrypted/game.cxi',
                         'Nintendo 3DS/_decrypted/game (DLC).cxi',
                         'Nintendo 3DS/_bios/seeddb.bin',
                         'Wii U/_decrypted/0005000012345678/code/game.rpx',
                         'Wii U/_decrypted/0005000e12345678/code/update.rpx']:
                path = nas / file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture')
            config_path = root / 'config.yml'
            config_path.write_text(config)
            code = source.replace('/romm/library', str(library)).replace(
                '/romm/config/config.yml', str(config_path))
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(code, 'build.py', 'exec'), {})
                exec(compile(code, 'build.py', 'exec'), {})  # idempotent rebuild
            self.assertEqual((library / 'Roms/GameCube/game.rvz').read_bytes(), b'fixture')
            self.assertTrue((library / 'Roms/PS2/game.bin').is_file())
            self.assertFalse((library / 'Roms/PS2/game.cue').exists())
            self.assertFalse((library / 'Roms/Xbox/game.iso.gz').exists())
            self.assertTrue((library / '_bios/Nintendo 3DS/seeddb.bin').is_file())
            self.assertTrue((library / 'Roms/Nintendo 3DS/game.cxi').is_file())
            self.assertFalse((library / 'Roms/Nintendo 3DS/game (DLC).cxi').exists())
            self.assertEqual([p.name for p in (library / 'Roms/Wii U').iterdir()],
                             ['0005000012345678'])
            self.assertTrue((nas / 'PS2/game.cue').is_file())  # source untouched

if __name__ == '__main__':
    unittest.main()
