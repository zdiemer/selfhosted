import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('exporter', Path(__file__).resolve().parents[1] / 'scripts/exporter.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class BackupTest(unittest.TestCase):
    def test_missing_schedule_and_success_fail_closed(self):
        with patch.dict(os.environ, BACKUP_NAMESPACES='["missing"]'):
            self.assertEqual(m.backup_metrics([], [], 1000), ['home_backup_age_seconds{namespace="missing"} 1000'])
    def test_only_successful_completion_resets_age(self):
        schedules = [{'metadata':{'namespace':'test'}, 'spec':{'backup':{ 'schedule':'weekly'}}}]
        def backup(reason, stamp):
            return {'metadata':{'namespace':'test'},'status':{'conditions':[{'type':'Completed','status':'True','reason':reason,'lastTransitionTime':stamp}]}}
        with patch.dict(os.environ, BACKUP_NAMESPACES='[]'):
            result = m.backup_metrics(schedules,[backup('Succeeded','1970-01-01T00:10:00Z'),backup('Failed','1970-01-01T00:15:00Z')],1000)
        self.assertEqual(result,['home_backup_age_seconds{namespace="test"} 400.0'])

if __name__ == '__main__': unittest.main()
