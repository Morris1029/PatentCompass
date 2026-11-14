"""HTTP adapter for the same capability services used by MCP."""

import hashlib
from pathlib import Path

from fastapi import File, Request, Response, UploadFile

from app import capability_service
from app.errors import AppError
from app.files import store_bytes
from app.resource_service import idempotent


def register(app):
    from app.main import DB

    @app.get("/api/capabilities", tags=["capabilities"])
    def capabilities():
        return capability_service.catalogue()

    @app.post("/api/capabilities/files", status_code=201, tags=["capabilities"])
    def upload_capability_file(request: Request, response: Response, session: DB, file: UploadFile = File(...)):
        name = Path(file.filename or "").name
        content = file.file.read(20 * 1024 * 1024 + 1)
        if Path(name).suffix.lower() not in {".xlsx", ".doc", ".docx"} or not content or len(content) > 20 * 1024 * 1024:
            raise AppError(422, "INVALID_CAPABILITY_FILE", "请上传不超过 20MB 的 .xlsx、.doc 或 .docx 文件")

        def store():
            row = store_bytes(session, content, name, "raw")
            return {"file_id": row.id, "filename": row.original_name, "size": row.size,
                    "sha256": row.sha256, "media_type": row.media_type, "download_url": f"/api/files/{row.id}"}

        result, response.status_code = idempotent(session, "capability:upload",
            request.headers.get("Idempotency-Key") or request.state.request_id,
            {"filename": name, "sha256": hashlib.sha256(content).hexdigest()}, store, 201)
        return result

    @app.post("/api/capabilities/{name}/invoke", tags=["capabilities"], responses={202: {"description": "已创建持久任务"}})
    def invoke_capability(name: str, body: dict, request: Request, response: Response, session: DB):
        result, response.status_code = capability_service.invoke(session, name, body,
            request.headers.get("Idempotency-Key"))
        return result
