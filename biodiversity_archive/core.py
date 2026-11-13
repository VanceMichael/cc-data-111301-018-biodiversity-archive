from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET


def inspect_file(path):
    with zipfile.ZipFile(Path(path)) as archive:
        raw = archive.read('meta.xml')
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('描述文件包含外部实体')
        root = ET.fromstring(raw)
        namespace = {'d':'http://rs.tdwg.org/dwc/text/'}
        descriptors = []
        for item in root:
            descriptors.append({'kind':item.tag.rsplit('}',1)[-1], 'row_type':item.get('rowType'),
                                'locations':[entry.text for entry in item.findall('./d:files/d:location',namespace)]})
    return {'descriptors':descriptors}
