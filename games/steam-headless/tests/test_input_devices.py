import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

module = runpy.run_path(str(Path(__file__).parents[1] / 'files/input-devices.py'))

class InputTests(unittest.TestCase):
    def test_exposes_only_virtual_input_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            classes = root / 'class/input'
            classes.mkdir(parents=True)
            for name, kind, dev in [('event0', 'physical', '13:64'), ('event3', 'virtual', '13:67'), ('js0', 'virtual', '13:0'), ('event4', 'virtual', '8:0')]:
                target = root / 'devices' / kind / 'input' / name
                target.mkdir(parents=True)
                (target / 'dev').write_text(dev)
                (classes / name).symlink_to(target)
            self.assertEqual(module['discover'](classes), {'event3': os.makedev(13, 67), 'js0': os.makedev(13, 0)})
            (classes / 'event3' / 'dev').unlink()
            self.assertNotIn('event3', module['discover'](classes))

    def test_missing_runtime_rule_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit):
            module['main']()

if __name__ == '__main__': unittest.main()
