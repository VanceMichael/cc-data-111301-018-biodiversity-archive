"""Darwin Core 文本资料包解析。

所有读取规则（文件位置、编码、分隔符、引号、忽略行数、默认值）都来自
meta.xml，不假设固定英文表头。术语一律保留完整 URI，未知术语不丢弃，
同一术语出现多列时保留全部取值，绝不转成会互相覆盖的字典。

核心行的 id、对外 occurrenceID、扩展行的 coreid 是三种不同的键：
id/coreid 只是资料包内部的本地关联键，其行号身份只在对应数据集与发布内有效。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

DWCA_NAMESPACE = 'http://rs.tdwg.org/dwc/text/'
NS = {'d': DWCA_NAMESPACE}

# 没有术语描述（或超出描述范围）的列，用这个占位 URN 保留原始值。
UNKNOWN_TERM = 'urn:biodiversity-archive:unknown-column#%s'

# GBIF 风格的转义写法，例如 fieldsTerminatedBy="\\t"。
_MARKER_ESCAPES = {'\\t': '\t', '\\n': '\n', '\\r': '\r'}


def _decode_marker(value):
    if value is None:
        return None
    return _MARKER_ESCAPES.get(value, value)


def _safe_location(name):
    """资料内路径只允许相对路径，禁止 zip-slip 之类的越界读取。"""
    if not name:
        raise ValueError('meta.xml 中的文件位置为空')
    if name.startswith(('/', '\\')) or ':' in name:
        raise ValueError('资料文件路径必须是包内相对路径: %r' % name)
    parts = name.replace('\\', '/').split('/')
    if any(part in ('', '..') for part in parts):
        raise ValueError('资料文件路径越界: %r' % name)
    return name


@dataclass
class FieldSpec:
    """meta.xml 中的一个 field 描述。index 为 None 表示默认值字段。"""
    index: int | None
    term: str | None
    default: str | None


@dataclass
class TableSpec:
    kind: str                      # core / extension
    row_type: str | None
    encoding: str
    delimiter: str
    line_terminator: str
    quote_char: str                # 空字符串表示不加引号
    ignore_header: int
    locations: list[str]
    key_index: int | None          # core 的 id 或 extension 的 coreid 列
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass
class RowValue:
    position: int
    term: str
    value: str


@dataclass
class DataRow:
    """一行资料。values 保留列的顺序与重复术语，不做字典折叠。"""
    row_type: str | None
    source_file: str
    source_line: int               # 物理文件行号（含引号内换行的跨度）
    ordinal: int                   # 该表内从 1 开始的数据序号
    local_key: str | None          # core: id 值；extension: coreid 值
    values: list[RowValue] = field(default_factory=list)

    def term_map(self):
        """术语 -> 取值列表。同一术语多列时列表里有多个值。"""
        result = {}
        for item in self.values:
            result.setdefault(item.term, []).append(item.value)
        return result


@dataclass
class ParsedTable:
    spec: TableSpec
    rows: list[DataRow] = field(default_factory=list)


@dataclass
class ParsedArchive:
    source_name: str
    core: ParsedTable
    extensions: list[ParsedTable]

    def all_tables(self):
        return [self.core, *self.extensions]


def parse_meta(raw):
    """解析 meta.xml 字节，返回 (核心表描述, 扩展表描述列表)。"""
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('描述文件包含外部实体')
    root = ET.fromstring(raw)
    core_elements = root.findall('d:core', NS)
    if len(core_elements) != 1:
        raise ValueError('meta.xml 必须恰好描述一个 core 表')
    core_spec = _table_spec(core_elements[0], 'core')
    extension_specs = [_table_spec(element, 'extension')
                       for element in root.findall('d:extension', NS)]
    return core_spec, extension_specs


def _table_spec(element, kind):
    key_element = element.find('d:id' if kind == 'core' else 'd:coreid', NS)
    key_index = int(key_element.get('index')) if key_element is not None and key_element.get('index') is not None else None
    locations = [entry.text for entry in element.findall('./d:files/d:location', NS)]
    if not locations:
        raise ValueError('%s 表没有声明数据文件' % kind)
    fields = []
    for field_element in element.findall('d:field', NS):
        index_attr = field_element.get('index')
        fields.append(FieldSpec(
            index=int(index_attr) if index_attr is not None else None,
            term=field_element.get('term'),
            default=field_element.get('default'),
        ))
    delimiter = _decode_marker(element.get('fieldsTerminatedBy')) or ','
    if len(delimiter) != 1:
        raise ValueError('不支持多字符分隔符: %r' % delimiter)
    return TableSpec(
        kind=kind,
        row_type=element.get('rowType'),
        encoding=element.get('encoding') or 'UTF-8',
        delimiter=delimiter,
        line_terminator=_decode_marker(element.get('linesTerminatedBy')) or '\n',
        quote_char=_decode_marker(element.get('fieldsEnclosedBy')) or '',
        ignore_header=int(element.get('ignoreHeaderLines') or 0),
        locations=[_safe_location(name) for name in locations],
        key_index=key_index,
        fields=fields,
    )


def _build_row(spec, cells, source_file, source_line, ordinal):
    local_key = None
    if spec.key_index is not None and spec.key_index < len(cells):
        local_key = cells[spec.key_index]
    values = []
    indexed = sorted(
        (field_spec for field_spec in spec.fields if field_spec.index is not None),
        key=lambda item: item.index)
    max_index = -1
    # 按列的物理顺序输出，重复术语因此保持各列原始次序，位置即列号。
    for field_spec in indexed:
        max_index = max(max_index, field_spec.index)
        cell = cells[field_spec.index] if field_spec.index < len(cells) else ''
        if cell == '' and field_spec.default is not None:
            cell = field_spec.default
        term = field_spec.term or UNKNOWN_TERM % field_spec.index
        values.append(RowValue(field_spec.index, term, cell))
    # 超出 meta 描述的列也不丢弃，保留为未知术语。
    for extra_index in range(max_index + 1, len(cells)):
        values.append(RowValue(extra_index, UNKNOWN_TERM % extra_index, cells[extra_index]))
    # 无 index 的默认值字段，对每行生效，排在物理列之后。
    position = max(max_index, len(cells) - 1) + 1
    for field_spec in spec.fields:
        if field_spec.index is None and field_spec.default is not None:
            term = field_spec.term or UNKNOWN_TERM % 'default'
            values.append(RowValue(position, term, field_spec.default))
            position += 1
    return DataRow(
        row_type=spec.row_type,
        source_file=source_file,
        source_line=source_line,
        ordinal=ordinal,
        local_key=local_key,
        values=values,
    )


def read_table(archive, spec):
    """按 TableSpec 从 ZIP 中读取一张表（可能由多个文件组成）。"""
    reader_kwargs = {'delimiter': spec.delimiter}
    if spec.quote_char:
        reader_kwargs['quotechar'] = spec.quote_char
    else:
        # 没有引号字符时，引号按普通字符处理，不做引用解析。
        reader_kwargs['quoting'] = csv.QUOTE_NONE
        reader_kwargs['quotechar'] = '"'
    table = ParsedTable(spec=spec)
    ordinal = 0
    for location in spec.locations:
        with archive.open(location) as raw_stream:
            text = io.TextIOWrapper(raw_stream, encoding=spec.encoding, newline='')
            reader = csv.reader(text, **reader_kwargs)
            for _ in range(spec.ignore_header):
                try:
                    next(reader)
                except StopIteration:
                    break
            for cells in reader:
                if not cells:
                    continue  # 空行不产生资料
                ordinal += 1
                table.rows.append(_build_row(spec, cells, location, reader.line_num, ordinal))
    return table


def parse_archive(source):
    """解析整个资料包，返回核心表与全部扩展表。"""
    path = Path(source)
    with zipfile.ZipFile(path) as archive:
        try:
            raw = archive.read('meta.xml')
        except KeyError:
            raise ValueError('资料包缺少 meta.xml')
        core_spec, extension_specs = parse_meta(raw)
        names = set(archive.namelist())
        for spec in [core_spec, *extension_specs]:
            missing = [name for name in spec.locations if name not in names]
            if missing:
                raise ValueError('数据文件不存在: %s' % ', '.join(missing))
        core = read_table(archive, core_spec)
        extensions = [read_table(archive, spec) for spec in extension_specs]
    return ParsedArchive(source_name=path.name, core=core, extensions=extensions)


def validate(parsed):
    """数据质量校验。问题保留而不是丢弃，绝不做内连接假装正常。

    返回问题列表，每条含 kind、table、row_type、source_file、source_line、
    local_key、message。
    """
    issues = []
    first_seen = {}
    occurrence_seen = {}
    for row in parsed.core.rows:
        if not row.local_key:
            issues.append(_issue('core_key_missing', 'core', row, '核心行缺少 id 本地键'))
            continue
        if row.local_key in first_seen:
            earlier = first_seen[row.local_key]
            issues.append(_issue(
                'duplicate_core_key', 'core', row,
                '核心本地键 %r 重复，首次出现于 %s:%s'
                % (row.local_key, earlier.source_file, earlier.source_line)))
        else:
            first_seen[row.local_key] = row
        occurrence_values = row.term_map().get('http://rs.tdwg.org/dwc/terms/occurrenceID', [])
        occurrence_id = next((value for value in occurrence_values if value), None)
        if occurrence_id is not None:
            if occurrence_id in occurrence_seen:
                issues.append(_issue(
                    'duplicate_occurrence_id', 'core', row,
                    '对外 occurrenceID %r 在同一发布内重复' % occurrence_id))
            else:
                occurrence_seen[occurrence_id] = row
    core_ids = set(first_seen)
    for table in parsed.extensions:
        for row in table.rows:
            if row.local_key not in core_ids:
                issues.append(_issue(
                    'orphan_extension', 'extension', row,
                    '扩展行引用的核心本地键 %r 不存在（缺父记录的孤立扩展）'
                    % row.local_key))
    return issues


def _issue(kind, table_kind, row, message):
    return {
        'kind': kind,
        'table': table_kind,
        'row_type': row.row_type,
        'source_file': row.source_file,
        'source_line': row.source_line,
        'local_key': row.local_key,
        'message': message,
    }


def media_fingerprint(row):
    """扩展行跨发布的内容指纹：扩展行没有稳定业务键，按内容比对。"""
    fields = tuple(sorted((term, tuple(values)) for term, values in row.term_map().items()))
    return row.row_type, fields
