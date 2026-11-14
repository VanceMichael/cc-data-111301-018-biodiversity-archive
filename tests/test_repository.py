import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from biodiversity_archive.archive import archive_file
from biodiversity_archive.dwca import parse_archive, validate
from biodiversity_archive.repository import Repository

from tests.fixture_builder import build_archive

FIXTURE_1 = Path('fixtures/archive-1.zip')
FIXTURE_2 = Path('fixtures/archive-2.zip')
OCC = 'http://rs.tdwg.org/dwc/terms/occurrenceID'
NAME = 'http://rs.tdwg.org/dwc/terms/scientificName'
URL = 'http://purl.org/dc/terms/identifier'
LICENSE = 'http://purl.org/dc/terms/license'
MULTIMEDIA = 'http://rs.gbif.org/terms/1.0/Multimedia'

META = '''<archive xmlns="http://rs.tdwg.org/dwc/text/">
<core encoding="UTF-8" fieldsTerminatedBy="&#9;" linesTerminatedBy="&#10;" fieldsEnclosedBy="" ignoreHeaderLines="1" rowType="http://rs.tdwg.org/dwc/terms/Occurrence">
<files><location>obs.tsv</location></files>
<id index="0"/>
<field index="1" term="http://rs.tdwg.org/dwc/terms/occurrenceID"/>
<field index="2" term="http://rs.tdwg.org/dwc/terms/scientificName"/>
<field term="http://rs.tdwg.org/dwc/terms/basisOfRecord" default="HumanObservation"/>
</core>
<extension encoding="UTF-8" fieldsTerminatedBy="," linesTerminatedBy="&#10;" fieldsEnclosedBy="&quot;" ignoreHeaderLines="1" rowType="http://rs.gbif.org/terms/1.0/Multimedia">
<files><location>media.csv</location></files>
<coreid index="0"/>
<field index="1" term="http://purl.org/dc/terms/identifier"/>
<field index="2" term="http://purl.org/dc/terms/license"/>
</extension>
</archive>'''


def make_zip(directory, name, obs_lines, media_lines, meta=META):
    files = {'obs.tsv': obs_lines, 'media.csv': media_lines}
    return build_archive(Path(directory) / name, meta, files)


def ingest_zip(repo, raw_dir, path, dataset):
    captured = archive_file(path, raw_dir)
    parsed = parse_archive(path)
    return repo.ingest(parsed, captured['sha256'], captured['bytes'], dataset,
                       validate(parsed)), captured


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.raw_dir = self.root / 'raw'
        self.repo = Repository(self.root / 'catalog.sqlite3')

    def tearDown(self):
        self.repo.close()
        self._temp.cleanup()

    def test_observation_counted_once_with_multiple_media(self):
        path = make_zip(
            self.root, 'p1.zip',
            '内部键\t观察编号\t物种名\n1\tocc-a\t鸟甲\n2\tocc-b\t鸟乙\n',
            '核心键,资料地址,授权\n1,https://example.invalid/a,CC0\n'
            '1,https://example.invalid/b,CC0\n')
        receipt, _ = ingest_zip(self.repo, self.raw_dir, path, 'site-a')
        self.assertEqual(receipt['core_count'], 2)
        self.assertEqual(receipt['extension_count'], 2)
        listing = self.repo.list_observations('site-a')
        self.assertEqual(len(listing), 2)
        first = next(item for item in listing if item['occurrence_id'] == 'occ-a')
        self.assertEqual(first['extension_count'], 2)
        view = self.repo.get_observation('site-a', 'occ-a')
        self.assertEqual(len(view['core']), 1)
        self.assertEqual(len(view['extensions']), 2)
        self.assertEqual(
            view['core'][0]['fields'][OCC], ['occ-a'])
        urls = [extension['fields'][URL][0] for extension in view['extensions']]
        self.assertEqual(urls, ['https://example.invalid/a', 'https://example.invalid/b'])
        # 回看得到原始文件与物理行号。
        self.assertEqual(view['core'][0]['source_file'], 'obs.tsv')
        self.assertEqual(view['core'][0]['source_line'], 2)
        self.assertEqual(view['extensions'][0]['source_line'], 2)

    def test_orphan_extension_persists_and_is_queryable(self):
        receipt, _ = ingest_zip(
            self.repo, self.raw_dir, FIXTURE_2, 'site-a')
        self.assertIn('orphan_extension', receipt['issue_summary'])
        orphans = self.repo.list_orphans('site-a')
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]['coreid'], 'missing')
        self.assertEqual(orphans[0]['fields'][URL],
                         ['https://example.invalid/c'])
        issues = self.repo.list_issues('site-a')
        self.assertEqual([issue['kind'] for issue in issues], ['orphan_extension'])
        # 正常观察不受孤立扩展影响，不做内连接。
        view = self.repo.get_observation('site-a', 'occ-a')
        self.assertEqual(len(view['extensions']), 2)

    def test_duplicate_core_key_is_persisted_with_issue(self):
        path = make_zip(
            self.root, 'dup.zip',
            '内部键\t观察编号\t物种名\n1\tocc-a\t鸟甲\n1\tocc-c\t鸟丙\n',
            '核心键,资料地址,授权\n1,https://example.invalid/a,CC0\n')
        receipt, _ = ingest_zip(self.repo, self.raw_dir, path, 'site-a')
        self.assertIn('duplicate_core_key', receipt['issue_summary'])
        # 同一本地键两行都在，occurrenceID 不同也能分别回看。
        self.assertEqual(len(self.repo.list_observations('site-a')), 2)
        view_a = self.repo.get_observation('site-a', 'occ-a')
        view_c = self.repo.get_observation('site-a', 'occ-c')
        self.assertEqual(view_a['core'][0]['local_key'], '1')
        self.assertEqual(view_c['core'][0]['local_key'], '1')
        self.assertEqual(
            view_a['extensions'][0]['fields'][URL], ['https://example.invalid/a'])

    def test_new_publication_replaces_current_but_keeps_history(self):
        first, cap1 = ingest_zip(self.repo, self.raw_dir, FIXTURE_1, 'site-a')
        second, cap2 = ingest_zip(self.repo, self.raw_dir, FIXTURE_2, 'site-a')
        self.assertTrue(first['is_new'])
        self.assertTrue(second['is_new'])
        self.assertEqual(self.repo.current_sha256('site-a'), cap2['sha256'])
        publications = self.repo.list_publications('site-a')
        self.assertEqual([item['sha256'] for item in publications],
                         [cap1['sha256'], cap2['sha256']])
        # 旧发布仍可按摘要查询，其中没有孤立扩展。
        self.assertEqual(self.repo.list_orphans('site-a', cap1['sha256']), [])
        self.assertEqual(len(self.repo.list_orphans('site-a', cap2['sha256'])), 1)
        # 重复接收同一字节内容是幂等的，不新增发布。
        again, _ = ingest_zip(self.repo, self.raw_dir, FIXTURE_2, 'site-a')
        self.assertFalse(again['is_new'])
        self.assertEqual(len(self.repo.list_publications('site-a')), 2)

    def test_other_datasets_are_not_affected(self):
        ingest_zip(self.repo, self.raw_dir, FIXTURE_1, 'site-a')
        other, cap_other = ingest_zip(self.repo, self.raw_dir, FIXTURE_2, 'site-b')
        ingest_zip(self.repo, self.raw_dir, FIXTURE_2, 'site-a')
        # site-b 的当前发布与历史不受 site-a 更新影响。
        self.assertEqual(self.repo.current_sha256('site-b'), cap_other['sha256'])
        self.assertEqual(len(self.repo.list_publications('site-b')), 1)
        datasets = {item['dataset_key']: item for item in self.repo.list_datasets()}
        self.assertEqual(set(datasets), {'site-a', 'site-b'})

    def test_empty_extension_keeps_core_observations(self):
        path = make_zip(
            self.root, 'nomedia.zip',
            '内部键\t观察编号\t物种名\n1\tocc-a\t鸟甲\n',
            '核心键,资料地址,授权\n')
        receipt, _ = ingest_zip(self.repo, self.raw_dir, path, 'site-a')
        self.assertEqual(receipt['core_count'], 1)
        self.assertEqual(receipt['extension_count'], 0)
        listing = self.repo.list_observations('site-a')
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['extension_count'], 0)
        self.assertEqual(self.repo.get_observation('site-a', 'occ-a')['extensions'], [])

    def test_local_keys_are_scoped_per_publication(self):
        # 两个数据集都使用本地键 "1"，扩展行不得串到另一家保护地。
        p1 = make_zip(self.root, 'a.zip',
                      '内部键\t观察编号\t物种名\n1\tocc-a\t鸟甲\n',
                      '核心键,资料地址,授权\n1,https://example.invalid/a,CC0\n')
        p2 = make_zip(self.root, 'b.zip',
                      '内部键\t观察编号\t物种名\n1\tocc-b\t鸟乙\n',
                      '核心键,资料地址,授权\n1,https://example.invalid/b,CC-BY\n')
        ingest_zip(self.repo, self.raw_dir, p1, 'site-a')
        ingest_zip(self.repo, self.raw_dir, p2, 'site-b')
        view_a = self.repo.get_observation('site-a', 'occ-a')
        view_b = self.repo.get_observation('site-b', 'occ-b')
        self.assertEqual(view_a['extensions'][0]['fields'][LICENSE], ['CC0'])
        self.assertEqual(view_b['extensions'][0]['fields'][LICENSE], ['CC-BY'])

    def test_diff_distinguishes_observation_and_media_changes(self):
        # 核心行与 fixture-2 完全一致，只改动扩展，用于验证“仅附属资料变化”。
        media_only = make_zip(
            self.root, 'media-only.zip',
            '内部键\t观察编号\t物种名\n1\tocc-a\t虚构鸟种甲\n2\tocc-b\t虚构鸟种乙\n',
            '核心键,资料地址,授权\n1,https://example.invalid/a,CC0\n'
            '1,https://example.invalid/b,CC0\n'
            '1,https://example.invalid/d,CC0\n')
        old_receipt, old_cap = ingest_zip(self.repo, self.raw_dir, FIXTURE_2, 'site-a')
        new_receipt, new_cap = ingest_zip(
            self.repo, self.raw_dir, media_only, 'site-a')
        report = self.repo.diff_publications('site-a', old_cap['sha256'], new_cap['sha256'])
        self.assertTrue(report['is_media_only'])
        self.assertEqual(report['observation_changes']['added'], [])
        self.assertEqual(report['observation_changes']['removed'], [])
        self.assertEqual(report['observation_changes']['changed'], [])
        self.assertIn('occ-a', report['observation_changes']['unchanged'])
        entry = next(e for e in report['media_changes'] if e['occurrence_id'] == 'occ-a')
        self.assertEqual(entry['media_added'], 1)
        # 旧发布的孤立图片消失，体现在 orphan_changes 而不是观察删除。
        self.assertEqual(report['totals']['orphan_removed'], 1)

        changed = make_zip(
            self.root, 'changed.zip',
            '内部键\t观察编号\t物种名\n1\tocc-a\t虚构鸟种甲改名\n2\tocc-b\t虚构鸟种乙\n',
            '核心键,资料地址,授权\n1,https://example.invalid/a,CC0\n'
            '1,https://example.invalid/b,CC0\n')
        changed_receipt, changed_cap = ingest_zip(
            self.repo, self.raw_dir, changed, 'site-a')
        report2 = self.repo.diff_publications('site-a', new_cap['sha256'], changed_cap['sha256'])
        self.assertFalse(report2['is_media_only'])
        changed_occ = report2['observation_changes']['changed']
        self.assertEqual([item['occurrence_id'] for item in changed_occ], ['occ-a'])
        entry = next(e for e in report2['media_changes'] if e['occurrence_id'] == 'occ-a')
        self.assertEqual(entry['media_removed'], 1)

    def test_diff_detects_added_and_removed_observations(self):
        smaller = make_zip(
            self.root, 'smaller.zip',
            '内部键\t观察编号\t物种名\n1\tocc-a\t虚构鸟种甲\n',
            '核心键,资料地址,授权\n1,https://example.invalid/a,CC0\n')
        _, cap1 = ingest_zip(self.repo, self.raw_dir, FIXTURE_1, 'site-a')
        _, cap2 = ingest_zip(self.repo, self.raw_dir, smaller, 'site-a')
        report = self.repo.diff_publications('site-a', cap1['sha256'], cap2['sha256'])
        self.assertEqual(
            [item['local_keys'] for item in report['observation_changes']['removed']],
            [['2']])
        back, cap3 = ingest_zip(self.repo, self.raw_dir, FIXTURE_1, 'site-a')
        report2 = self.repo.diff_publications('site-a', cap2['sha256'], cap3['sha256'])
        self.assertEqual(
            [item['local_keys'] for item in report2['observation_changes']['added']],
            [['2']])


if __name__ == '__main__':
    unittest.main()
