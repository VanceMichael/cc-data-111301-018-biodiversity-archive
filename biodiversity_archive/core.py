"""资料包结构预览。

inspect_file 保留原有 descriptors 输出形状，同时把 meta.xml 声明的读取规则
（编码、分隔符、引号、忽略行数、默认值字段）一并暴露，说明解析没有硬编码表头。
"""
from pathlib import Path
import zipfile

from .dwca import parse_archive, parse_meta


def _descriptor(spec):
    return {
        'kind': spec.kind,
        'row_type': spec.row_type,
        'locations': list(spec.locations),
        'encoding': spec.encoding,
        'delimiter': spec.delimiter,
        'line_terminator': spec.line_terminator,
        'quote_char': spec.quote_char,
        'ignore_header_lines': spec.ignore_header,
        'key_index': spec.key_index,
        'fields': [
            {'index': field_spec.index, 'term': field_spec.term,
             'default': field_spec.default}
            for field_spec in spec.fields
        ],
    }


def inspect_file(path):
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        raw = archive.read('meta.xml')
        core_spec, extension_specs = parse_meta(raw)
    return {'descriptors': [_descriptor(core_spec),
                            *[_descriptor(spec) for spec in extension_specs]]}


def preview_file(path):
    """结构 + 行数 + 数据质量问题的完整预览，不写入任何仓储。"""
    from .dwca import validate
    parsed = parse_archive(path)
    issues = validate(parsed)
    return {
        'source_name': parsed.source_name,
        'descriptors': [_descriptor(table.spec) for table in parsed.all_tables()],
        'core_rows': len(parsed.core.rows),
        'extension_rows': {
            table.spec.row_type: len(table.rows) for table in parsed.extensions
        },
        'issues': issues,
    }
