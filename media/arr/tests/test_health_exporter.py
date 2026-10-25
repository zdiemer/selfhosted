import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location('arr_exporter', Path(__file__).resolve().parents[1] / 'scripts/health-exporter.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

class HealthTest(unittest.TestCase):
    def test_counts_and_safe_bounded_identifiers(self):
        checks = [{'type':'warning','source':'IndexerStatusCheck','message':'http://secret:password@example.test'},
                  {'type':'warning','source':'IndexerStatusCheck'},
                  {'type':'error','source':'DownloadClientCheck'}]
        xml = MagicMock()
        xml.getroot.return_value.findtext.return_value = 'test-key'
        with patch.dict(exporter.os.environ, {'ARR_APP':'sonarr','ARR_PORT':'8989'}), patch.object(exporter.ET, 'parse', return_value=xml), patch.object(exporter.urllib.request, 'urlopen') as urlopen:
            urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(checks).encode())
            result = exporter.collect()
        self.assertIn('severity="warning",checks="IndexerStatusCheck"} 2', result)
        self.assertIn('severity="error",checks="DownloadClientCheck"} 1', result)
        self.assertNotIn('password', result)
        self.assertNotIn('test-key', result)

if __name__ == '__main__': unittest.main()
