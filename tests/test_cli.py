import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = PROJECT_ROOT / 'tests' / '_isolate.py'


class CommandTests(unittest.TestCase):
    def run_cli(self, *arguments):
        process = subprocess.run(
            [sys.executable, '-I', str(BOOTSTRAP), '-m', *arguments],
            capture_output=True, text=True, timeout=30,
            cwd=PROJECT_ROOT)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_preview_and_raw_capture(self):
        result = self.run_cli('demo')
        self.assertIn('观察回看', result)
        self.assertGreater(result['首份接收']['bytes'], 0)
        self.assertEqual(len(result['首份接收']['sha256']), 64)
        # 演示路径里第二份发布带孤立扩展，第三份修掉，差异只是附属资料。
        diff = result['新旧发布差异']
        self.assertTrue(diff['is_media_only'])
        self.assertEqual(diff['totals']['orphan_removed'], 1)
        # 另一家保护地始终只有一份发布，未被甲的更新波及。
        self.assertEqual(result['另一保护地']['core_count'], 2)
        other = next(item for item in result['数据集目录']
                     if item['dataset_key'] == '保护地乙')
        self.assertEqual(other['publication_count'], 1)
        observation = result['观察回看']
        self.assertEqual(observation['occurrence_id'], 'occ-a')
        self.assertGreaterEqual(len(observation['extensions']), 3)

    def test_ingest_and_query_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'catalog.sqlite3')
            raw = str(Path(directory) / 'raw')
            receipt = self.run_cli('ingest', 'fixtures/archive-2.zip',
                                   '--dataset', 'site-a', '--db', db,
                                   '--archive', raw)
            self.assertTrue(receipt['接收']['is_new'])
            observations = self.run_cli('observations', '--dataset', 'site-a', '--db', db)
            self.assertEqual(len(observations), 2)
            self.assertEqual(
                next(item for item in observations
                     if item['occurrence_id'] == 'occ-a')['extension_count'], 2)
            view = self.run_cli('observation', '--dataset', 'site-a',
                                '--occurrence-id', 'occ-a', '--db', db)
            self.assertEqual(view['core'][0]['source_line'], 2)
            orphans = self.run_cli('orphans', '--dataset', 'site-a', '--db', db)
            self.assertEqual(orphans[0]['coreid'], 'missing')
            issues = self.run_cli('issues', '--dataset', 'site-a', '--db', db)
            self.assertEqual(issues[0]['kind'], 'orphan_extension')
            datasets = self.run_cli('datasets', '--db', db)
            self.assertEqual([item['dataset_key'] for item in datasets], ['site-a'])
            publications = self.run_cli('publications', '--dataset', 'site-a', '--db', db)
            self.assertEqual(len(publications), 1)
            preview = self.run_cli('preview', 'fixtures/archive-2.zip')
            self.assertEqual(preview['core_rows'], 2)
            self.assertTrue(any(i['kind'] == 'orphan_extension'
                                for i in preview['issues']))

    def test_legacy_commands_still_work(self):
        inspect_result = self.run_cli('inspect', 'fixtures/archive-1.zip')
        self.assertEqual(inspect_result['descriptors'][0]['kind'], 'core')
        self.assertEqual(inspect_result['descriptors'][1]['locations'], ['media.csv'])
        with tempfile.TemporaryDirectory() as directory:
            capture = self.run_cli('capture', 'fixtures/archive-1.zip',
                                   '--archive', str(Path(directory) / 'raw'))
            self.assertEqual(len(capture['sha256']), 64)


if __name__ == '__main__':
    unittest.main()
