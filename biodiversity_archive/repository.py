"""采集仓储：数据集 -> 多份不可变发布 -> 核心行/扩展行/问题。

身份边界：
* 发布以 (数据集, 资料包 SHA-256) 为身份，同一字节内容在同一数据集内只接收一次；
  不同数据集即使收到字节相同的资料包，行与问题也各自独立存储；
* 核心行身份为 (数据集, 发布摘要, 核心表序号)，本地 id 只是包内关联键；
* 扩展行身份为 (数据集, 发布摘要, 扩展表序号, 表内序号)，coreid 只在同一发布内引用；
* occurrenceID 是对外观察标识，与本地 id 分栏存储，绝不混用。

接收语义：新发布只更新所属数据集的当前指针，其他数据集与该数据集的历史发布
都保留。扩展为空不会删除任何核心观察；问题（缺父记录、重复键、孤立扩展）
作为数据留下并可查询，不做内连接。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .dwca import media_fingerprint, validate

OCCURRENCE_TERM = 'http://rs.tdwg.org/dwc/terms/occurrenceID'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    dataset_key TEXT NOT NULL REFERENCES datasets(dataset_key),
    sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    core_row_type TEXT,
    core_count INTEGER NOT NULL,
    extension_count INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (dataset_key, sha256)
);
CREATE TABLE IF NOT EXISTS current_publications (
    dataset_key TEXT PRIMARY KEY REFERENCES datasets(dataset_key),
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS core_rows (
    dataset_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    local_key TEXT,
    occurrence_id TEXT,
    row_hash TEXT NOT NULL,
    PRIMARY KEY (dataset_key, sha256, ordinal)
);
CREATE TABLE IF NOT EXISTS core_row_fields (
    dataset_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    position INTEGER NOT NULL,
    term TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extension_rows (
    dataset_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    extension_ordinal INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    row_type TEXT,
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    coreid TEXT,
    orphan INTEGER NOT NULL,
    row_hash TEXT NOT NULL,
    PRIMARY KEY (dataset_key, sha256, extension_ordinal, ordinal)
);
CREATE TABLE IF NOT EXISTS extension_row_fields (
    dataset_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    extension_ordinal INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    position INTEGER NOT NULL,
    term TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    dataset_key TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    issue_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    table_kind TEXT NOT NULL,
    row_type TEXT,
    source_file TEXT,
    source_line INTEGER,
    local_key TEXT,
    message TEXT NOT NULL,
    PRIMARY KEY (dataset_key, sha256, issue_id)
);
CREATE INDEX IF NOT EXISTS idx_core_occurrence
    ON core_rows(dataset_key, sha256, occurrence_id);
CREATE INDEX IF NOT EXISTS idx_core_local
    ON core_rows(dataset_key, sha256, local_key);
CREATE INDEX IF NOT EXISTS idx_extension_coreid
    ON extension_rows(dataset_key, sha256, coreid);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_hash(row_type, term_map):
    payload = [row_type]
    for term in sorted(term_map):
        payload.append([term, term_map[term]])
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode('utf-8')).hexdigest()


def _jsonable(value):
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


class Repository:
    def __init__(self, path):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute('PRAGMA foreign_keys = ON')
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- 接收

    def ingest(self, parsed, sha256, size, dataset_key, issues=None):
        """接收一份解析后的发布并登记为数据集的当前发布。

        同摘要在同一数据集内重复接收是幂等的：不重复写行，只刷新当前指针与
        时间戳。返回接收凭证，包含是否为新发布、行数与问题汇总。
        """
        if issues is None:
            issues = validate(parsed)
        conn = self._conn
        now = _now()
        with conn:
            conn.execute(
                'INSERT INTO datasets(dataset_key, created_at, updated_at) VALUES(?,?,?) '
                'ON CONFLICT(dataset_key) DO UPDATE SET updated_at=excluded.updated_at',
                (dataset_key, now, now))
            existing = conn.execute(
                'SELECT 1 FROM publications WHERE dataset_key=? AND sha256=?',
                (dataset_key, sha256)).fetchone()
            is_new = existing is None
            if is_new:
                self._insert_publication(parsed, sha256, size, dataset_key, now, issues)
            conn.execute(
                'INSERT INTO current_publications(dataset_key, sha256) VALUES(?,?) '
                'ON CONFLICT(dataset_key) DO UPDATE SET sha256=excluded.sha256',
                (dataset_key, sha256))
        issue_summary = {}
        for issue in issues:
            issue_summary[issue['kind']] = issue_summary.get(issue['kind'], 0) + 1
        return {
            'dataset_key': dataset_key,
            'sha256': sha256,
            'source_name': parsed.source_name,
            'bytes': size,
            'is_new': is_new,
            'core_count': len(parsed.core.rows),
            'extension_count': sum(len(table.rows) for table in parsed.extensions),
            'issue_summary': issue_summary,
            'issues': issues,
        }

    def _insert_publication(self, parsed, sha256, size, dataset_key, now, issues):
        conn = self._conn
        conn.execute(
            'INSERT INTO publications(dataset_key, sha256, source_name, bytes,'
            ' core_row_type, core_count, extension_count, received_at)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (dataset_key, sha256, parsed.source_name, size, parsed.core.spec.row_type,
             len(parsed.core.rows),
             sum(len(table.rows) for table in parsed.extensions), now))
        core_ids = {row.local_key for row in parsed.core.rows if row.local_key}
        for row in parsed.core.rows:
            term_map = row.term_map()
            occurrence_values = term_map.get(OCCURRENCE_TERM, [])
            occurrence_id = next((value for value in occurrence_values if value), None)
            conn.execute(
                'INSERT INTO core_rows(dataset_key, sha256, ordinal, source_file,'
                ' source_line, local_key, occurrence_id, row_hash)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (dataset_key, sha256, row.ordinal, row.source_file, row.source_line,
                 row.local_key, occurrence_id,
                 _row_hash(row.row_type, term_map)))
            conn.executemany(
                'INSERT INTO core_row_fields(dataset_key, sha256, ordinal, position,'
                ' term, value) VALUES(?,?,?,?,?,?)',
                [(dataset_key, sha256, row.ordinal, value.position,
                  value.term, value.value) for value in row.values])
        for extension_ordinal, table in enumerate(parsed.extensions, start=1):
            for row in table.rows:
                term_map = row.term_map()
                orphan = 0 if row.local_key in core_ids else 1
                conn.execute(
                    'INSERT INTO extension_rows(dataset_key, sha256, extension_ordinal,'
                    ' ordinal, row_type, source_file, source_line, coreid, orphan, row_hash)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (dataset_key, sha256, extension_ordinal, row.ordinal, row.row_type,
                     row.source_file, row.source_line, row.local_key, orphan,
                     _row_hash(row.row_type, term_map)))
                conn.executemany(
                    'INSERT INTO extension_row_fields(dataset_key, sha256,'
                    ' extension_ordinal, ordinal, position, term, value)'
                    ' VALUES(?,?,?,?,?,?,?)',
                    [(dataset_key, sha256, extension_ordinal, row.ordinal,
                      value.position, value.term, value.value)
                     for value in row.values])
        for issue_id, issue in enumerate(issues, start=1):
            conn.execute(
                'INSERT INTO issues(dataset_key, sha256, issue_id, kind, table_kind,'
                ' row_type, source_file, source_line, local_key, message)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                (dataset_key, sha256, issue_id, issue['kind'], issue['table'],
                 issue['row_type'], issue['source_file'], issue['source_line'],
                 issue['local_key'], issue['message']))

    # ---------------------------------------------------------------- 目录

    def list_datasets(self):
        rows = self._conn.execute(
            'SELECT d.dataset_key, d.created_at, d.updated_at, c.sha256,'
            ' (SELECT COUNT(*) FROM publications p WHERE p.dataset_key=d.dataset_key)'
            ' FROM datasets d LEFT JOIN current_publications c ON c.dataset_key=d.dataset_key'
            ' ORDER BY d.dataset_key').fetchall()
        return [{'dataset_key': key, 'created_at': created, 'updated_at': updated,
                 'current_sha256': current, 'publication_count': count}
                for key, created, updated, current, count in rows]

    def list_publications(self, dataset_key):
        self._require_dataset(dataset_key)
        rows = self._conn.execute(
            'SELECT sha256, source_name, bytes, core_count, extension_count, received_at'
            ' FROM publications WHERE dataset_key=? ORDER BY received_at, sha256',
            (dataset_key,)).fetchall()
        return [{'sha256': sha, 'source_name': name, 'bytes': size,
                 'core_count': core, 'extension_count': extension, 'received_at': received}
                for sha, name, size, core, extension, received in rows]

    def current_sha256(self, dataset_key):
        self._require_dataset(dataset_key)
        row = self._conn.execute(
            'SELECT sha256 FROM current_publications WHERE dataset_key=?',
            (dataset_key,)).fetchone()
        if row is None:
            raise ValueError('数据集 %r 还没有任何发布' % dataset_key)
        return row[0]

    def _require_dataset(self, dataset_key):
        row = self._conn.execute(
            'SELECT 1 FROM datasets WHERE dataset_key=?', (dataset_key,)).fetchone()
        if row is None:
            raise ValueError('未知数据集: %r' % dataset_key)

    def _require_publication(self, dataset_key, sha256):
        row = self._conn.execute(
            'SELECT 1 FROM publications WHERE dataset_key=? AND sha256=?',
            (dataset_key, sha256)).fetchone()
        if row is None:
            raise ValueError('数据集 %r 没有发布 %s' % (dataset_key, sha256))

    def _resolve_sha(self, dataset_key, sha256):
        sha = sha256 if sha256 else self.current_sha256(dataset_key)
        self._require_publication(dataset_key, sha)
        return sha

    # ---------------------------------------------------------------- 查询

    def list_observations(self, dataset_key, sha256=None):
        """观察清单：一条核心观察只算一次，图片再多也不重复计数。"""
        sha = self._resolve_sha(dataset_key, sha256)
        rows = self._conn.execute(
            'SELECT occurrence_id, local_key, COUNT(*),'
            " MIN(source_file||':'||source_line)"
            ' FROM core_rows WHERE dataset_key=? AND sha256=?'
            ' GROUP BY occurrence_id, local_key ORDER BY MIN(ordinal)',
            (dataset_key, sha)).fetchall()
        result = []
        for occurrence_id, local_key, core_count, first_source in rows:
            extension_count = 0
            if local_key is not None:
                extension_count = self._conn.execute(
                    'SELECT COUNT(*) FROM extension_rows'
                    ' WHERE dataset_key=? AND sha256=? AND coreid=?',
                    (dataset_key, sha, local_key)).fetchone()[0]
            result.append({
                'dataset_key': dataset_key,
                'publication': sha,
                'occurrence_id': occurrence_id,
                'local_key': local_key,
                'core_row_count': core_count,
                'extension_count': extension_count,
                'first_source': first_source,
            })
        return result

    def get_observation(self, dataset_key, occurrence_id, sha256=None):
        """从对外 occurrenceID 回看观察本身、全部关联资料与原始行号。"""
        sha = self._resolve_sha(dataset_key, sha256)
        core_rows = self._conn.execute(
            'SELECT ordinal, source_file, source_line, local_key, row_hash FROM core_rows'
            ' WHERE dataset_key=? AND sha256=? AND occurrence_id=? ORDER BY ordinal',
            (dataset_key, sha, occurrence_id)).fetchall()
        if not core_rows:
            raise ValueError('发布 %s 中找不到 occurrenceID=%r' % (sha, occurrence_id))
        core_view = []
        local_keys = set()
        for ordinal, source_file, source_line, local_key, row_hash in core_rows:
            local_keys.add(local_key)
            core_view.append({
                'ordinal': ordinal,
                'source_file': source_file,
                'source_line': source_line,
                'local_key': local_key,
                'row_hash': row_hash,
                'fields': self._core_fields(dataset_key, sha, ordinal),
            })
        extensions = []
        for local_key in sorted(key for key in local_keys if key is not None):
            extensions.extend(self._extensions_for(dataset_key, sha, local_key))
        return {
            'dataset_key': dataset_key,
            'publication': sha,
            'occurrence_id': occurrence_id,
            'core': core_view,
            'extensions': extensions,
        }

    def _core_fields(self, dataset_key, sha, ordinal):
        rows = self._conn.execute(
            'SELECT term, value FROM core_row_fields'
            ' WHERE dataset_key=? AND sha256=? AND ordinal=? ORDER BY position',
            (dataset_key, sha, ordinal)).fetchall()
        term_map = {}
        for term, value in rows:
            term_map.setdefault(term, []).append(value)
        return term_map

    def _extension_fields(self, dataset_key, sha, extension_ordinal, ordinal):
        rows = self._conn.execute(
            'SELECT term, value FROM extension_row_fields'
            ' WHERE dataset_key=? AND sha256=? AND extension_ordinal=? AND ordinal=?'
            ' ORDER BY position',
            (dataset_key, sha, extension_ordinal, ordinal)).fetchall()
        term_map = {}
        for term, value in rows:
            term_map.setdefault(term, []).append(value)
        return term_map

    def _extensions_for(self, dataset_key, sha, coreid):
        rows = self._conn.execute(
            'SELECT extension_ordinal, ordinal, row_type, source_file, source_line, coreid,'
            ' orphan, row_hash FROM extension_rows'
            ' WHERE dataset_key=? AND sha256=? AND coreid=?'
            ' ORDER BY extension_ordinal, ordinal',
            (dataset_key, sha, coreid)).fetchall()
        return [self._extension_view(dataset_key, sha, row) for row in rows]

    def _extension_view(self, dataset_key, sha, row):
        extension_ordinal, ordinal, row_type, source_file, source_line, coreid, orphan, row_hash = row
        return {
            'extension_ordinal': extension_ordinal,
            'ordinal': ordinal,
            'row_type': row_type,
            'source_file': source_file,
            'source_line': source_line,
            'coreid': coreid,
            'orphan': bool(orphan),
            'row_hash': row_hash,
            'fields': self._extension_fields(
                dataset_key, sha, extension_ordinal, ordinal),
        }

    def list_orphans(self, dataset_key, sha256=None):
        """孤立扩展：coreid 在本发布核心表中不存在，保留而不丢弃。"""
        sha = self._resolve_sha(dataset_key, sha256)
        rows = self._conn.execute(
            'SELECT extension_ordinal, ordinal, row_type, source_file, source_line, coreid,'
            ' orphan, row_hash FROM extension_rows'
            ' WHERE dataset_key=? AND sha256=? AND orphan=1'
            ' ORDER BY extension_ordinal, ordinal',
            (dataset_key, sha)).fetchall()
        return [self._extension_view(dataset_key, sha, row) for row in rows]

    def list_issues(self, dataset_key, sha256=None):
        sha = self._resolve_sha(dataset_key, sha256)
        rows = self._conn.execute(
            'SELECT issue_id, kind, table_kind, row_type, source_file, source_line, local_key,'
            ' message FROM issues WHERE dataset_key=? AND sha256=? ORDER BY issue_id',
            (dataset_key, sha)).fetchall()
        return [{'issue_id': issue_id, 'kind': kind, 'table': table_kind,
                 'row_type': row_type, 'source_file': source_file,
                 'source_line': source_line, 'local_key': local_key, 'message': message}
                for issue_id, kind, table_kind, row_type, source_file, source_line,
                local_key, message in rows]

    # ---------------------------------------------------------------- 发布对比

    def diff_publications(self, dataset_key, old_sha, new_sha):
        """对比同一数据集的两份发布，区分观察本身变化与附属资料变化。"""
        self._require_publication(dataset_key, old_sha)
        self._require_publication(dataset_key, new_sha)
        old_core = self._core_index(dataset_key, old_sha)
        new_core = self._core_index(dataset_key, new_sha)
        old_ext = self._extension_index(dataset_key, old_sha)
        new_ext = self._extension_index(dataset_key, new_sha)

        old_named = {key: item for key, item in old_core.items() if key is not None}
        new_named = {key: item for key, item in new_core.items() if key is not None}
        added, removed, changed, unchanged = [], [], [], []
        media_changes = []

        for occurrence_id in sorted(set(old_named) | set(new_named)):
            old_item = old_named.get(occurrence_id)
            new_item = new_named.get(occurrence_id)
            if old_item is None:
                added.append(self._core_brief(new_item))
                media_changes.append(self._media_entry(
                    occurrence_id, [], new_item['local_keys'], old_ext, new_ext))
                continue
            if new_item is None:
                removed.append(self._core_brief(old_item))
                media_changes.append(self._media_entry(
                    occurrence_id, old_item['local_keys'], [], old_ext, new_ext))
                continue
            if old_item['hash_multiset'] != new_item['hash_multiset']:
                changed.append({'occurrence_id': occurrence_id,
                                'old': self._core_brief(old_item),
                                'new': self._core_brief(new_item)})
            else:
                unchanged.append(occurrence_id)
            media_changes.append(self._media_entry(
                occurrence_id, old_item['local_keys'], new_item['local_keys'],
                old_ext, new_ext))

        # 没有 occurrenceID 的核心行按内容哈希多重集比较。
        old_loose = _fingerprint_multiset(self._loose_hashes(old_core))
        new_loose = _fingerprint_multiset(self._loose_hashes(new_core))
        loose_added = _multiset_diff(new_loose, old_loose)
        loose_removed = _multiset_diff(old_loose, new_loose)

        old_orphans = _fingerprint_multiset(old_ext.get(None, []))
        new_orphans = _fingerprint_multiset(new_ext.get(None, []))
        orphan_added = _multiset_diff(new_orphans, old_orphans)
        orphan_removed = _multiset_diff(old_orphans, new_orphans)

        media_added_total = sum(entry['media_added'] for entry in media_changes)
        media_removed_total = sum(entry['media_removed'] for entry in media_changes)
        observation_changed = bool(added or removed or changed
                                   or loose_added or loose_removed)
        media_changed = bool(media_added_total or media_removed_total
                             or orphan_added or orphan_removed)
        return {
            'dataset_key': dataset_key,
            'old_publication': old_sha,
            'new_publication': new_sha,
            'observation_changes': {
                'added': added,
                'removed': removed,
                'changed': changed,
                'unchanged': unchanged,
                'unnamed_added': loose_added,
                'unnamed_removed': loose_removed,
            },
            'media_changes': media_changes,
            'orphan_changes': {'added': orphan_added, 'removed': orphan_removed},
            'totals': {
                'media_added': media_added_total,
                'media_removed': media_removed_total,
                'orphan_added': len(orphan_added),
                'orphan_removed': len(orphan_removed),
            },
            'is_media_only': (not observation_changed) and media_changed,
            'issues': {
                'old': self._issue_summary(dataset_key, old_sha),
                'new': self._issue_summary(dataset_key, new_sha),
            },
        }

    def _core_index(self, dataset_key, sha):
        rows = self._conn.execute(
            'SELECT ordinal, occurrence_id, local_key, row_hash FROM core_rows'
            ' WHERE dataset_key=? AND sha256=? ORDER BY ordinal',
            (dataset_key, sha)).fetchall()
        index = {}
        for ordinal, occurrence_id, local_key, row_hash in rows:
            item = index.setdefault(occurrence_id, {'local_keys': set(),
                                                     'hash_multiset': [],
                                                     'rows': []})
            item['local_keys'].add(local_key)
            item['hash_multiset'].append(row_hash)
            item['rows'].append({'ordinal': ordinal, 'local_key': local_key,
                                 'row_hash': row_hash})
        for item in index.values():
            item['hash_multiset'].sort()
        return index

    def _extension_index(self, dataset_key, sha):
        rows = self._conn.execute(
            'SELECT extension_ordinal, ordinal, row_type, source_file, source_line, coreid,'
            ' orphan, row_hash FROM extension_rows'
            ' WHERE dataset_key=? AND sha256=? ORDER BY extension_ordinal, ordinal',
            (dataset_key, sha)).fetchall()
        index = {}
        orphans = []
        for row in rows:
            view = self._extension_view(dataset_key, sha, row)
            fingerprint = _jsonable(media_fingerprint(_FingerprintRow(view)))
            # 元组顺序：extension_ordinal, ordinal, row_type, source_file,
            # source_line, coreid, orphan, row_hash —— coreid 在索引 5。
            index.setdefault(row[5], []).append(fingerprint)
            if row[6]:
                orphans.append(fingerprint)
        index[None] = orphans
        return index

    def _media_entry(self, occurrence_id, old_local_keys, new_local_keys,
                     old_ext, new_ext):
        old_media = _fingerprint_multiset(_gather_media(old_ext, old_local_keys))
        new_media = _fingerprint_multiset(_gather_media(new_ext, new_local_keys))
        media_added = _multiset_diff(new_media, old_media)
        media_removed = _multiset_diff(old_media, new_media)
        return {
            'occurrence_id': occurrence_id,
            'media_added': len(media_added),
            'media_removed': len(media_removed),
            'added': media_added,
            'removed': media_removed,
        }

    def _core_brief(self, item):
        return {'local_keys': sorted(key for key in item['local_keys'] if key is not None),
                'row_count': len(item['rows'])}

    def _loose_hashes(self, core_index):
        item = core_index.get(None)
        return item['hash_multiset'] if item else []

    def _issue_summary(self, dataset_key, sha):
        rows = self._conn.execute(
            'SELECT kind, COUNT(*) FROM issues WHERE dataset_key=? AND sha256=?'
            ' GROUP BY kind', (dataset_key, sha)).fetchall()
        return {kind: count for kind, count in rows}


class _FingerprintRow:
    """把查询视图适配成 media_fingerprint 需要的 row.term_map() 接口。"""

    def __init__(self, view):
        self.row_type = view['row_type']
        self._term_map = view['fields']

    def term_map(self):
        return self._term_map


def _gather_media(extension_index, local_keys):
    gathered = []
    for key in local_keys:
        gathered.extend(extension_index.get(key, []))
    return gathered


def _fingerprint_multiset(media):
    return sorted(json.dumps(_jsonable(item), ensure_ascii=False, sort_keys=True)
                  for item in media)


def _multiset_diff(left, right):
    """left 相对 right 多出的成员（多重集差）。成员均为 JSON 编码字符串。"""
    remaining = list(right)
    diff = []
    for member in left:
        try:
            remaining.remove(member)
        except ValueError:
            diff.append(json.loads(member))
    return diff
