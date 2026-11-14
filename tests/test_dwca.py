import tempfile
import unittest
import zipfile
from pathlib import Path

from biodiversity_archive.core import inspect_file, preview_file
from biodiversity_archive.dwca import parse_archive, validate

from tests.fixture_builder import GBK_META, QUOTED_META, build_archive

FIXTURE_1 = Path('fixtures/archive-1.zip')
FIXTURE_2 = Path('fixtures/archive-2.zip')


class ParserTests(unittest.TestCase):
    def test_meta_driven_rules_are_exposed(self):
        result = inspect_file(FIXTURE_1)
        core, media = result['descriptors']
        self.assertEqual(core['encoding'], 'UTF-8')
        self.assertEqual(core['delimiter'], '\t')
        self.assertEqual(core['quote_char'], '')
        self.assertEqual(core['ignore_header_lines'], 1)
        self.assertEqual(media['delimiter'], ',')
        self.assertEqual(media['quote_char'], '"')
        default_field = [f for f in core['fields']
                         if f['term'] == 'http://rs.tdwg.org/dwc/terms/basisOfRecord'][0]
        self.assertIsNone(default_field['index'])
        self.assertEqual(default_field['default'], 'HumanObservation')

    def test_default_values_and_unknown_terms_are_kept(self):
        parsed = parse_archive(FIXTURE_1)
        row = parsed.core.rows[0]
        term_map = row.term_map()
        self.assertEqual(
            term_map['http://rs.tdwg.org/dwc/terms/basisOfRecord'], ['HumanObservation'])
        self.assertEqual(
            term_map['http://rs.tdwg.org/dwc/terms/occurrenceID'], ['occ-a'])
        # 原始物理行号与表内序号都保留。
        self.assertEqual(row.source_line, 2)
        self.assertEqual(row.ordinal, 1)
        self.assertEqual(row.local_key, '1')

    def test_repeated_terms_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = build_archive(
                Path(directory) / 'repeated.zip',
                QUOTED_META,
                {
                    'core-a.csv': 'id,occ,note1,note2\n1,occ-x,第一处,第二处\n',
                    'core-b.csv': 'id,occ,note1,note2\n2,occ-y,第三处,第四处\n',
                    'media.csv': 'coreid,url,license\n1,https://example.invalid/1,CC0\n',
                })
            parsed = parse_archive(path)
        self.assertEqual(len(parsed.core.rows), 2)
        term_map = parsed.core.rows[0].term_map()
        self.assertEqual(
            term_map['http://example.org/terms/fieldNotes'], ['第一处', '第二处'])
        # 多文件合并后序号连续，来源文件名分别保留。
        self.assertEqual(parsed.core.rows[0].source_file, 'core-a.csv')
        self.assertEqual(parsed.core.rows[1].source_file, 'core-b.csv')
        self.assertEqual([row.ordinal for row in parsed.core.rows], [1, 2])
        # 原始值是有序列表而非字典，顺序与列顺序一致；位置即物理列号。
        positions = [(v.position, v.term, v.value) for v in parsed.core.rows[0].values]
        self.assertEqual(positions[0][1:],
                         ('http://rs.tdwg.org/dwc/terms/occurrenceID', 'occ-x'))
        self.assertEqual(positions[1][1:],
                         ('http://example.org/terms/fieldNotes', '第一处'))
        self.assertEqual(positions[2][1:],
                         ('http://example.org/terms/fieldNotes', '第二处'))

    def test_encoding_and_ignored_header_lines_come_from_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            content = ('说明行一\n说明行二\n'
                       '1|occ-甲|虚构鸟种\n').encode('gbk')
            path = build_archive(Path(directory) / 'gbk.zip', GBK_META,
                                 {'obs.txt': content})
            parsed = parse_archive(path)
        self.assertEqual(len(parsed.core.rows), 1)
        row = parsed.core.rows[0]
        self.assertEqual(
            row.term_map()['http://rs.tdwg.org/dwc/terms/scientificName'], ['虚构鸟种'])
        # 两行表头被忽略，数据行物理行号为 3。
        self.assertEqual(row.source_line, 3)

    def test_quoted_newlines_do_not_break_source_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {
                'core-a.csv': 'id,occ,note,extra\n1,occ-x,"跨行\n备注",v\n',
                'core-b.csv': 'id,occ,note,extra\n',
                'media.csv': 'coreid,url,license\n1,https://example.invalid/x,CC0\n',
            }
            path = build_archive(Path(directory) / 'quoted.zip', QUOTED_META, files)
            parsed = parse_archive(path)
        self.assertEqual(len(parsed.core.rows), 1)
        self.assertEqual(parsed.core.rows[0].source_line, 3)
        self.assertEqual(
            parsed.core.rows[0].term_map()['http://example.org/terms/fieldNotes'],
            ['跨行\n备注', 'v'])

    def test_extra_columns_are_kept_as_unknown_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {
                'core-a.csv': 'id,occ,note,extra,surprise\n1,occ-x,n,e,未知列\n',
                'core-b.csv': 'id,occ,note,extra\n',
                'media.csv': 'coreid,url,license\n',
            }
            path = build_archive(Path(directory) / 'extra.zip', QUOTED_META, files)
            parsed = parse_archive(path)
        terms = [value.term for value in parsed.core.rows[0].values]
        self.assertTrue(any(term.startswith('urn:biodiversity-archive:unknown-column#4')
                            for term in terms))
        unknown = [v for v in parsed.core.rows[0].values
                   if v.term.startswith('urn:biodiversity-archive')]
        self.assertEqual(unknown[0].value, '未知列')

    def test_three_key_kinds_are_distinct(self):
        parsed = parse_archive(FIXTURE_1)
        self.assertEqual(parsed.core.rows[0].local_key, '1')
        self.assertEqual(
            parsed.core.rows[0].term_map()[
                'http://rs.tdwg.org/dwc/terms/occurrenceID'], ['occ-a'])
        extension_row = parsed.extensions[0].rows[0]
        self.assertEqual(extension_row.local_key, '1')
        # coreid 不是观察身份：同一 coreid 下有两张图片，但核心观察只有一条。
        same_parent = [r for r in parsed.extensions[0].rows if r.local_key == '1']
        self.assertEqual(len(same_parent), 2)
        self.assertEqual(len(parsed.core.rows), 2)

    def test_orphan_extension_is_reported_not_dropped(self):
        parsed = parse_archive(FIXTURE_2)
        issues = validate(parsed)
        kinds = {issue['kind'] for issue in issues}
        self.assertIn('orphan_extension', kinds)
        orphan = next(issue for issue in issues if issue['kind'] == 'orphan_extension')
        self.assertEqual(orphan['local_key'], 'missing')
        self.assertEqual(orphan['source_file'], 'media.csv')
        self.assertEqual(orphan['source_line'], 4)
        # 孤立行仍然在解析结果里。
        self.assertIn('missing', [r.local_key for r in parsed.extensions[0].rows])
        # 干净资料包没有问题。
        self.assertEqual(validate(parse_archive(FIXTURE_1)), [])

    def test_duplicate_core_key_is_reported(self):
        meta = QUOTED_META
        with tempfile.TemporaryDirectory() as directory:
            files = {
                'core-a.csv': 'id,occ,note,extra\n1,occ-x,a,b\n1,occ-y,c,d\n',
                'core-b.csv': 'id,occ,note,extra\n',
                'media.csv': 'coreid,url,license\n1,u,CC0\n',
            }
            path = build_archive(Path(directory) / 'dup.zip', meta, files)
            issues = validate(parse_archive(path))
        dupes = [issue for issue in issues if issue['kind'] == 'duplicate_core_key']
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0]['source_line'], 3)

    def test_preview_includes_counts_and_issues(self):
        result = preview_file(FIXTURE_2)
        self.assertEqual(result['core_rows'], 2)
        self.assertEqual(result['extension_rows'],
                         {'http://rs.gbif.org/terms/1.0/Multimedia': 3})
        self.assertTrue(any(i['kind'] == 'orphan_extension' for i in result['issues']))

    def test_zip_slip_locations_are_rejected(self):
        evil_meta = ('<archive xmlns="http://rs.tdwg.org/dwc/text/">'
                     '<core encoding="UTF-8" fieldsTerminatedBy="," rowType="x">'
                     '<files><location>../escape.csv</location></files>'
                     '<id index="0"/></core></archive>')
        with tempfile.TemporaryDirectory() as directory:
            path = build_archive(Path(directory) / 'evil.zip', evil_meta,
                                 {'../escape.csv': 'x\n'})
            with self.assertRaises(ValueError):
                parse_archive(path)

    def test_xxe_meta_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'xxe.zip'
            with zipfile.ZipFile(path, 'w') as writer:
                writer.writestr('meta.xml',
                                '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "v">]>'
                                '<archive xmlns="http://rs.tdwg.org/dwc/text/"/>')
            with self.assertRaises(ValueError):
                parse_archive(path)

    def test_missing_data_file_is_an_error_not_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = build_archive(
                Path(directory) / 'missing.zip',
                QUOTED_META,
                {'core-a.csv': 'id,occ,note,extra\n',
                 'media.csv': 'coreid,url,license\n'})
            with self.assertRaises(ValueError):
                parse_archive(path)


if __name__ == '__main__':
    unittest.main()
