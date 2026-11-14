"""Word source parsing with original-file retention and stable source locations."""

import hashlib
import shutil  # Preserve the existing converter-discovery override surface.

from sqlalchemy import select

from patentcompass_core.documents import parse_docx, parse_document as _parse_source

from app.db import session_scope
from app.errors import AppError
from app.files import resolve_file
from app.models import Material, MaterialBlock, StoredFile


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
