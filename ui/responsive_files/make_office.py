"""Generate small OOXML fixtures using Python stdlib and the installed SheetJS theme."""
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
import base64
import posixpath
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
WIDE = 'W' * 80
OTHER = 'folder_' + 'm' * 93
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
REL_TYPE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'

def rels(items):
    return XML + f'<Relationships xmlns="{REL_NS}">' + ''.join(f'<Relationship Id="{ident}" Type="{REL_TYPE}{kind}" Target="{target}"/>' for ident, kind, target in items) + '</Relationships>'

def types(parts):
    return XML + f'<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>' + ''.join(f'<Override PartName="/{name}" ContentType="{mime}"/>' for name, mime in parts.items()) + '</Types>'

def write_zip(name, parts):
    with ZipFile(HERE / name, 'w', ZIP_DEFLATED) as archive:
        for path, content in parts.items():
            archive.writestr(path, content)

word_ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
paragraphs = [WIDE, 'First paragraph: a short sentence checks normal text at narrow widths.', 'Second paragraph: ' + OTHER]
word = XML + f'<w:document xmlns:w="{word_ns}"><w:body>'
for i, text in enumerate(paragraphs):
    props = '<w:pPr><w:keepNext/></w:pPr>' if i == 0 else ''
    word += f'<w:p>{props}<w:r><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="{32 if i == 0 else 24}"/></w:rPr><w:t>{escape(text)}</w:t></w:r></w:p>'
word += '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720" w:header="360" w:footer="360" w:gutter="0"/></w:sectPr></w:body></w:document>'
write_zip('responsive.docx', {
    '[Content_Types].xml': types({'word/document.xml': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'}),
    '_rels/.rels': rels([('rId1', 'officeDocument', 'word/document.xml')]),
    'word/document.xml': word,
})

# DrawingML themes are shared by Excel and PowerPoint. Reuse the complete theme
# emitted by the already-installed SheetJS version instead of inventing one.
theme_js = "const x=require('xlsx');const b=x.utils.book_new();x.utils.book_append_sheet(b,x.utils.aoa_to_sheet([['Fixture']]),'Sheet');process.stdout.write(x.write(b,{type:'buffer',bookType:'xlsx'}).toString('base64'));"
encoded = subprocess.check_output(['node', '-e', theme_js], cwd=ROOT / 'ui')
with ZipFile(BytesIO(base64.b64decode(encoded))) as archive:
    theme = archive.read('xl/theme/theme1.xml')

group = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
clrmap = '<p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/>'
ns = f'xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"'

def textbox(ident, name, texts, x, y, cx, cy, size):
    body = ''.join(f'<a:p><a:pPr/><a:r><a:rPr lang="en-US" sz="{size}"><a:solidFill><a:srgbClr val="302F2B"/></a:solidFill><a:latin typeface="Arial"/></a:rPr><a:t>{escape(text)}</a:t></a:r><a:endParaRPr lang="en-US"/></a:p>' for text in texts)
    return f'<p:sp><p:nvSpPr><p:cNvPr id="{ident}" name="{name}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr wrap="square" lIns="0" rIns="0" tIns="0" bIns="0"/><a:lstStyle/>{body}</p:txBody></p:sp>'

def slide(index):
    heading = WIDE if index == 1 else OTHER
    title = textbox(2, 'Long heading', [heading], 457200, 457200, 8229600, 1645920, 2200)
    body = textbox(3, 'Body', [f'Slide {index}: responsive viewer fixture.', 'This second paragraph checks a normal line beside the long heading.'], 457200, 2514600, 8229600, 1371600, 1400)
    circle = '<p:sp><p:nvSpPr><p:cNvPr id="4" name="Fixture marker"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="7772400" y="4114800"/><a:ext cx="457200" cy="457200"/></a:xfrm><a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="B7603C"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr></p:sp>'
    return XML + f'<p:sld {ns}><p:cSld name="Fixture {index}"><p:spTree>{group}{title}{body}{circle}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'

presentation = XML + f'<p:presentation {ns}><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rIdMaster"/></p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst><p:sldSz cx="9144000" cy="5143500" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
master = XML + f'<p:sldMaster {ns}><p:cSld name="Fixture master"><p:spTree>{group}</p:spTree></p:cSld>{clrmap}<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rIdLayout"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>'
layout = XML + f'<p:sldLayout {ns} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>{group}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
ct = {
    'ppt/presentation.xml': 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml',
    'ppt/presProps.xml': 'application/vnd.openxmlformats-officedocument.presentationml.presProps+xml',
    'ppt/slideMasters/slideMaster1.xml': 'application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml',
    'ppt/slideLayouts/slideLayout1.xml': 'application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml',
    'ppt/theme/theme1.xml': 'application/vnd.openxmlformats-officedocument.theme+xml',
    **{f'ppt/slides/slide{i}.xml': 'application/vnd.openxmlformats-officedocument.presentationml.slide+xml' for i in (1,2)},
}
parts = {
    '[Content_Types].xml': types(ct),
    '_rels/.rels': rels([('rId1', 'officeDocument', 'ppt/presentation.xml')]),
    'ppt/presentation.xml': presentation,
    'ppt/_rels/presentation.xml.rels': rels([('rIdMaster','slideMaster','slideMasters/slideMaster1.xml'),('rId1','slide','slides/slide1.xml'),('rId2','slide','slides/slide2.xml'),('rIdProps','presProps','presProps.xml')]),
    'ppt/presProps.xml': XML + f'<p:presentationPr {ns}/>',
    'ppt/slideMasters/slideMaster1.xml': master,
    'ppt/slideMasters/_rels/slideMaster1.xml.rels': rels([('rIdLayout','slideLayout','../slideLayouts/slideLayout1.xml'),('rIdTheme','theme','../theme/theme1.xml')]),
    'ppt/slideLayouts/slideLayout1.xml': layout,
    'ppt/slideLayouts/_rels/slideLayout1.xml.rels': rels([('rIdMaster','slideMaster','../slideMasters/slideMaster1.xml')]),
    'ppt/theme/theme1.xml': theme,
}
for i in (1,2):
    parts[f'ppt/slides/slide{i}.xml'] = slide(i)
    parts[f'ppt/slides/_rels/slide{i}.xml.rels'] = rels([('rIdLayout','slideLayout','../slideLayouts/slideLayout1.xml')])
write_zip('responsive.pptx', parts)

for name in ('responsive.docx', 'responsive.pptx'):
    with ZipFile(HERE / name) as archive:
        assert archive.testzip() is None, f'{name}: CRC failure'
        names = set(archive.namelist())
        xml_parts = {}
        for part in names:
            if part.endswith(('.xml','.rels')):
                xml_parts[part] = ET.fromstring(archive.read(part))
        for part, root in xml_parts.items():
            if not part.endswith('.rels'):
                continue
            base = '' if part == '_rels/.rels' else posixpath.dirname(part).removesuffix('/_rels')
            for rel in root:
                target = posixpath.normpath(posixpath.join(base, rel.attrib['Target']))
                assert target in names, f'{name}: missing relationship target {target}'
        for override in xml_parts['[Content_Types].xml']:
            if override.tag.endswith('Override'):
                assert override.attrib['PartName'].lstrip('/') in names
        content = ''.join(ET.tostring(root, encoding='unicode') for part, root in xml_parts.items() if part.startswith(('word/document','ppt/slides/slide')))
        assert WIDE in content
        assert OTHER in content
        if name.endswith('.pptx'):
            assert len(xml_parts['ppt/presentation.xml'].find(f'{{{P}}}sldIdLst')) == 2
        else:
            assert len(xml_parts['word/document.xml'].find(f'{{{word_ns}}}body').findall(f'{{{word_ns}}}p')) == 3
    print(f'{name}: {len(names)} ZIP entries; XML, CRC, content types, relationships and text pass; {(HERE/name).stat().st_size} bytes')
