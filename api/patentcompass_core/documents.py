"""Word parsing without application state or storage dependencies."""

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .contracts import CapabilityError


def parse_docx(path: Path) -> list[dict]:
    """Locations refer to XML paragraphs/cells, never to unverified Word pages."""
    try:
        document = Document(path)
        blocks = []
        paragraph_index, table_index = 0, 0
        for child in document.element.body.iterchildren():
            if child.tag == qn("w:p"):
                paragraph_index += 1
                text = Paragraph(child, document).text.strip()
                if text:
                    blocks.append({"kind": "paragraph", "text": text,
                                   "location": {"paragraph": paragraph_index}})
            elif child.tag == qn("w:tbl"):
                table_index += 1
                table = Table(child, document)
                # XML cells avoid repeating merged cells, as Table.rows.cells does.
                for row_index, row in enumerate(table._tbl.tr_lst, 1):
                    for cell_index, cell in enumerate(row.tc_lst, 1):
                        paragraphs = [Paragraph(p, table).text for p in cell.iter(qn("w:p"))]
                        text = "\n".join(part.strip() for part in paragraphs if part.strip())
                        if text:
                            blocks.append({"kind": "table_cell", "text": text,
                                           "location": {"table": table_index, "row": row_index,
                                                        "cell": cell_index}})
        if not blocks:
            raise CapabilityError(422, "MATERIAL_EMPTY", "Word 文件没有可提取的正文或表格文字")
        return [{**block, "ordinal": index,
                 "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}
                for index, block in enumerate(blocks, 1)]
    except CapabilityError:
        raise
    except Exception as exc:
        raise CapabilityError(422, "INVALID_WORD_DOCUMENT", "无法解析 Word 文件，请检查文件是否损坏或受密码保护") from exc



def parse_document(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.suffix.lower() == ".docx":
        return parse_docx(path)
    if path.suffix.lower() != ".doc":
        raise CapabilityError(422, "INVALID_MATERIAL_TYPE", "资料仅支持 .doc 和 .docx 文件")
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        raise CapabilityError(503, "WORD_CONVERTER_UNAVAILABLE", "Word 转换服务尚未就绪，请启动包含 LibreOffice 的 worker")
    with tempfile.TemporaryDirectory(prefix="patentcompass-word-") as directory:
        root = Path(directory)
        source = root / "source.doc"
        shutil.copyfile(path, source)
        try:
            result = subprocess.run([executable, "-env:UserInstallation=" + (root / "profile").as_uri(),
                                     "--headless", "--convert-to", "docx", "--outdir", str(root), str(source)],
                                    capture_output=True, timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CapabilityError(422, "WORD_CONVERSION_TIMEOUT", "Word 转换超时，可以重试或另存为 .docx 后上传") from exc
        converted = root / "source.docx"
        if result.returncode != 0 or not converted.is_file():
            raise CapabilityError(422, "WORD_CONVERSION_FAILED", "无法转换此 .doc 文件，请检查文件或另存为 .docx 后上传")
        return parse_docx(converted)

