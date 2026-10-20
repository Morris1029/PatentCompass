"""Word source parsing with original-file retention and stable source locations."""

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from sqlalchemy import select

from app.db import session_scope
from app.errors import AppError
from app.files import resolve_file
from app.models import Material, MaterialBlock, StoredFile


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
            raise AppError(422, "MATERIAL_EMPTY", "Word 文件没有可提取的正文或表格文字")
        return [{**block, "ordinal": index,
                 "text_sha256": hashlib.sha256(block["text"].encode()).hexdigest()}
                for index, block in enumerate(blocks, 1)]
    except AppError:
        raise
    except Exception as exc:
        raise AppError(422, "INVALID_WORD_DOCUMENT", "无法解析 Word 文件，请检查文件是否损坏或受密码保护") from exc


def _parse_source(path: Path) -> list[dict]:
    if path.suffix.lower() == ".docx":
        return parse_docx(path)
    if path.suffix.lower() != ".doc":
        raise AppError(422, "INVALID_MATERIAL_TYPE", "资料仅支持 .doc 和 .docx 文件")
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable:
        raise AppError(503, "WORD_CONVERTER_UNAVAILABLE", "Word 转换服务尚未就绪，请启动包含 LibreOffice 的 worker")
    with tempfile.TemporaryDirectory(prefix="patentcompass-word-") as directory:
        root = Path(directory)
        source = root / "source.doc"
        shutil.copyfile(path, source)
        try:
            result = subprocess.run([executable, "-env:UserInstallation=" + (root / "profile").as_uri(),
                                     "--headless", "--convert-to", "docx", "--outdir", str(root), str(source)],
                                    capture_output=True, timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AppError(422, "WORD_CONVERSION_TIMEOUT", "Word 转换超时，可以重试或另存为 .docx 后上传") from exc
        converted = root / "source.docx"
        if result.returncode != 0 or not converted.is_file():
            raise AppError(422, "WORD_CONVERSION_FAILED", "无法转换此 .doc 文件，请检查文件或另存为 .docx 后上传")
        return parse_docx(converted)


def parse_material(ctx) -> dict:
    material_id = ctx.payload["material_id"]

    def extract():
        with session_scope() as session:
            material = session.get(Material, material_id)
            if not material or material.project_id != ctx.project_id:
                raise AppError(404, "MATERIAL_NOT_FOUND", "资料不存在")
            source = session.get(StoredFile, material.file_id)
            path = resolve_file(source.key)
            if hashlib.sha256(path.read_bytes()).hexdigest() != source.sha256:
                raise AppError(409, "SOURCE_FILE_CHANGED", "原始文件校验失败")
        return {"blocks": _parse_source(path)}

    try:
        parsed = ctx.step("parse_material", extract)
        ctx.check_cancelled()

        def save():
            with session_scope() as session:
                material = session.scalar(select(Material).where(Material.id == material_id).with_for_update())
                ctx.guard(session)
                if material.status != "ready":
                    for block in parsed["blocks"]:
                        session.add(MaterialBlock(material_id=material_id, **block))
                    material.status, material.error = "ready", None
                return {"material_id": material_id, "file_id": material.file_id,
                        "version": material.version, "status": "ready", "block_count": len(parsed["blocks"])}

        result = ctx.step("save_material", save)
        ctx.emit("material.ready", result)
        return result
    except AppError as exc:
        with session_scope() as session:
            ctx.guard(session)
            material = session.get(Material, material_id)
            if material:
                material.status = "failed"
                material.error = {"code": exc.code, "message": exc.message}
        raise


def read_materials(session, material_ids: list[str], project_id: str | None = None) -> list[dict]:
    materials = []
    for material_id in dict.fromkeys(material_ids):
        material = session.get(Material, material_id)
        if not material or (project_id and material.project_id != project_id):
            raise AppError(404, "MATERIAL_NOT_FOUND", "资料不存在或不属于当前项目")
        if material.status != "ready":
            raise AppError(409, "MATERIAL_NOT_READY", "资料尚未解析完成", {"material_id": material_id})
        source = session.get(StoredFile, material.file_id)
        blocks = session.scalars(select(MaterialBlock).where(MaterialBlock.material_id == material_id)
                                 .order_by(MaterialBlock.ordinal)).all()
        materials.append({"material_id": material.id, "version": material.version, "file_id": source.id,
                          "name": source.original_name,
                          "blocks": [{"id": block.id, "ordinal": block.ordinal, "kind": block.kind,
                                      "text": block.text, "location": block.location} for block in blocks]})
    return materials
