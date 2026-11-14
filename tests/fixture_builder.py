"""测试辅助：在临时目录中按自定义 meta.xml 规则合成 Darwin Core 资料包。"""
import zipfile
from pathlib import Path


def build_archive(path, meta, files):
    """files: {包内路径: 字节串}。每个合成资料包都写到独立的新目录里。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as writer:
        writer.writestr('meta.xml', meta)
        for name, content in files.items():
            writer.writestr(name, content)
    return path


GBK_META = '''<archive xmlns="http://rs.tdwg.org/dwc/text/">
<core encoding="GBK" fieldsTerminatedBy="|" linesTerminatedBy="&#10;" fieldsEnclosedBy="" ignoreHeaderLines="2" rowType="http://rs.tdwg.org/dwc/terms/Occurrence">
<files><location>obs.txt</location></files>
<id index="0"/>
<field index="1" term="http://rs.tdwg.org/dwc/terms/occurrenceID"/>
<field index="2" term="http://rs.tdwg.org/dwc/terms/scientificName"/>
<field term="http://rs.tdwg.org/dwc/terms/basisOfRecord" default="HumanObservation"/>
</core>
</archive>'''

QUOTED_META = '''<archive xmlns="http://rs.tdwg.org/dwc/text/">
<core encoding="UTF-8" fieldsTerminatedBy="," linesTerminatedBy="&#10;" fieldsEnclosedBy="&quot;" ignoreHeaderLines="1" rowType="http://rs.tdwg.org/dwc/terms/Occurrence">
<files><location>core-a.csv</location><location>core-b.csv</location></files>
<id index="0"/>
<field index="1" term="http://rs.tdwg.org/dwc/terms/occurrenceID"/>
<field index="2" term="http://example.org/terms/fieldNotes"/>
<field index="3" term="http://example.org/terms/fieldNotes"/>
</core>
<extension encoding="UTF-8" fieldsTerminatedBy="," linesTerminatedBy="&#10;" fieldsEnclosedBy="&quot;" ignoreHeaderLines="1" rowType="http://rs.gbif.org/terms/1.0/Multimedia">
<files><location>media.csv</location></files>
<coreid index="0"/>
<field index="1" term="http://purl.org/dc/terms/identifier"/>
<field index="2" term="http://purl.org/dc/terms/license"/>
</extension>
</archive>'''
