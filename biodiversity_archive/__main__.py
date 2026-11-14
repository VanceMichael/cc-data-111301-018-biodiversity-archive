DESCRIPTION = 'Darwin Core 资料包的核心记录与扩展关系采集'
SAMPLE = 'fixtures/archive-1.zip'

import argparse
import json
from pathlib import Path
import tempfile

from .archive import archive_file
from .core import inspect_file, preview_file
from .dwca import parse_archive, validate
from .repository import Repository


def _print(result):
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _ingest(path, dataset, archive_dir, db_path):
    captured = archive_file(Path(path), Path(archive_dir))
    parsed = parse_archive(Path(path))
    issues = validate(parsed)
    with Repository(db_path) as repo:
        receipt = repo.ingest(parsed, captured['sha256'], captured['bytes'],
                              dataset, issues)
    return {'归档': captured, '接收': receipt}


def build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command')

    inspect_parser = sub.add_parser('inspect', help='只读 meta.xml 的结构预览')
    inspect_parser.add_argument('path', nargs='?', default=SAMPLE)

    preview_parser = sub.add_parser('preview', help='结构、行数与数据质量问题预览')
    preview_parser.add_argument('path', nargs='?', default=SAMPLE)

    capture_parser = sub.add_parser('capture', help='原始字节内容寻址归档')
    capture_parser.add_argument('path', nargs='?', default=SAMPLE)
    capture_parser.add_argument('--archive', default='.work/raw')

    ingest_parser = sub.add_parser('ingest', help='接收发布到指定数据集并设为当前')
    ingest_parser.add_argument('path')
    ingest_parser.add_argument('--dataset', required=True)
    ingest_parser.add_argument('--archive', default='.work/raw')
    ingest_parser.add_argument('--db', default='.work/catalog.sqlite3')

    sub.add_parser('datasets', help='数据集目录').add_argument('--db', default='.work/catalog.sqlite3')

    publications_parser = sub.add_parser('publications', help='数据集的历史发布（含已被替换的旧发布）')
    publications_parser.add_argument('--dataset', required=True)
    publications_parser.add_argument('--db', default='.work/catalog.sqlite3')

    observations_parser = sub.add_parser('observations', help='观察清单（一对多图片不重复计数）')
    observations_parser.add_argument('--dataset', required=True)
    observations_parser.add_argument('--publication')
    observations_parser.add_argument('--db', default='.work/catalog.sqlite3')

    observation_parser = sub.add_parser('observation', help='按 occurrenceID 回看观察、全部资料与原始行')
    observation_parser.add_argument('--dataset', required=True)
    observation_parser.add_argument('--occurrence-id', required=True)
    observation_parser.add_argument('--publication')
    observation_parser.add_argument('--db', default='.work/catalog.sqlite3')

    issues_parser = sub.add_parser('issues', help='发布的全部数据质量问题')
    issues_parser.add_argument('--dataset', required=True)
    issues_parser.add_argument('--publication')
    issues_parser.add_argument('--db', default='.work/catalog.sqlite3')

    orphans_parser = sub.add_parser('orphans', help='缺父记录的孤立扩展')
    orphans_parser.add_argument('--dataset', required=True)
    orphans_parser.add_argument('--publication')
    orphans_parser.add_argument('--db', default='.work/catalog.sqlite3')

    diff_parser = sub.add_parser('diff', help='同一数据集两份发布对比')
    diff_parser.add_argument('--dataset', required=True)
    diff_parser.add_argument('--old', help='旧发布摘要，缺省为历史中的上一份')
    diff_parser.add_argument('--new', help='新发布摘要，缺省为当前发布')
    diff_parser.add_argument('--db', default='.work/catalog.sqlite3')

    sub.add_parser('demo', help='临时目录端到端演示')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    command = args.command or 'demo'
    if command == 'inspect':
        _print(inspect_file(Path(args.path)))
        return
    if command == 'preview':
        _print(preview_file(Path(args.path)))
        return
    if command == 'capture':
        _print(archive_file(Path(args.path), Path(args.archive)))
        return
    if command == 'ingest':
        _print(_ingest(args.path, args.dataset, args.archive, args.db))
        return
    if command == 'demo':
        _print(_demo())
        return

    with Repository(args.db) as repo:
        if command == 'datasets':
            _print(repo.list_datasets())
        elif command == 'publications':
            _print(repo.list_publications(args.dataset))
        elif command == 'observations':
            _print(repo.list_observations(args.dataset, args.publication))
        elif command == 'observation':
            _print(repo.get_observation(args.dataset, args.occurrence_id, args.publication))
        elif command == 'issues':
            _print(repo.list_issues(args.dataset, args.publication))
        elif command == 'orphans':
            _print(repo.list_orphans(args.dataset, args.publication))
        elif command == 'diff':
            old = args.old
            new = args.new or repo.current_sha256(args.dataset)
            if old is None:
                publications = [item['sha256']
                                for item in repo.list_publications(args.dataset)
                                if item['sha256'] != new]
                if not publications:
                    raise SystemExit('数据集只有一份发布，无法对比')
                old = publications[-1]
            _print(repo.diff_publications(args.dataset, old, new))


def _demo():
    """端到端：同一保护地的两份发布 + 另一保护地，验证替换与隔离。"""
    import zipfile
    from .archive import read_object
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive_dir = root / 'raw'
        db_path = root / 'catalog.sqlite3'
        first = _ingest('fixtures/archive-1.zip', '保护地甲', archive_dir, db_path)
        second = _ingest('fixtures/archive-2.zip', '保护地甲', archive_dir, db_path)
        # 另一家保护地的发布，验证更新甲时不会影响乙。
        other = _ingest('fixtures/archive-1.zip', '保护地乙', archive_dir, db_path)
        corrected = root / 'archive-3.zip'
        _build_corrected_archive(read_object(archive_dir, first['归档']['sha256']),
                                 corrected)
        third = _ingest(corrected, '保护地甲', archive_dir, db_path)
        with Repository(db_path) as repo:
            observation = repo.get_observation('保护地甲', 'occ-a')
            diff = repo.diff_publications('保护地甲',
                                          second['接收']['sha256'],
                                          third['接收']['sha256'])
            datasets = repo.list_datasets()
        return {
            '首份接收': first['接收'],
            '第二份接收': second['接收'],
            '纠正后接收': third['接收'],
            '另一保护地': other['接收'],
            '数据集目录': datasets,
            '观察回看': observation,
            '新旧发布差异': diff,
        }


def _build_corrected_archive(raw_bytes, target):
    """在隔离目录里合成第三份发布：观察不变，只修掉孤立图片并新增一张图片。"""
    import zipfile
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / 'in.zip'
        source.write_bytes(raw_bytes)
        with zipfile.ZipFile(source) as archive:
            archive.extractall(Path(work) / 'unpacked')
        media = Path(work) / 'unpacked' / 'media.csv'
        lines = media.read_text(encoding='utf-8').splitlines()
        lines = [line for line in lines if not line.startswith('missing,')]
        lines.append('1,https://example.invalid/d,CC0')
        media.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as writer:
            for path in sorted((Path(work) / 'unpacked').rglob('*')):
                if path.is_file():
                    writer.write(path, path.relative_to(Path(work) / 'unpacked'))


if __name__ == '__main__':
    main()
