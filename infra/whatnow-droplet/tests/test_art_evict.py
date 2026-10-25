import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('art_evict', Path(__file__).parents[1] / 'files/art-evict.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class CacheGuardTests(unittest.TestCase):
    def test_all_tiers_oldest_first_without_touching_database_or_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            database = data / 'users.sqlite'
            database.write_bytes(b'important')
            art = data / 'art'
            for tier, age in [('header', 1), ('cover', 2), ('shot', 3)]:
                directory = art / tier
                directory.mkdir(parents=True)
                path = directory / 'image.jpg'
                path.write_bytes(b'x' * 4096)
                os.utime(path, (age, age))
            (art / 'link').symlink_to(database)
            (art / 'outside').symlink_to(data, target_is_directory=True)
            size = (art / 'shot/image.jpg').stat().st_blocks * 512
            count = guard.evict(art, ceiling=size, reserve=0, batch=1)
            self.assertEqual(count, 2)
            self.assertTrue((art / 'shot/image.jpg').exists())
            self.assertEqual(database.read_bytes(), b'important')
            self.assertTrue((art / 'link').is_symlink())

    def test_low_disk_reserve_evicts_even_below_cache_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image.jpg'
            path.write_bytes(b'x' * 4096)
            with patch.object(guard, 'available_bytes', side_effect=[0, 4096]):
                self.assertEqual(guard.evict(tmp, ceiling=100000, reserve=4096), 1)
            self.assertFalse(path.exists())

    def test_healthy_cache_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image.jpg'
            path.write_bytes(b'x')
            self.assertEqual(guard.evict(tmp, ceiling=100000, reserve=0), 0)
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
