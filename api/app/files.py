import hashlib
import mimetypes
from pathlib import Path

from app.config import get_settings
from app.errors import AppError
from app.models import StoredFile, new_id


def resolve_file(key: str) -> Path:
    root = get_settings().storage_root.resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise AppError(400, "INVALID_FILE_KEY", "文件路径超出存储目录")
    return path


def store_bytes(session, content: bytes, filename: str, kind: str, project_id=None, run_id=None) -> StoredFile:
    if run_id:
        from sqlalchemy import select
        previous = session.scalar(select(StoredFile).where(StoredFile.run_id == run_id))
        if previous:
            return previous
    file_id = new_id()
    suffix = Path(filename).suffix.lower()
    key = f"{kind}/{file_id}{suffix}"
    path = resolve_file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    row = StoredFile(id=file_id, owner_id=get_settings().dev_owner_id, project_id=project_id,
                     run_id=run_id, kind=kind, key=key, original_name=Path(filename).name,
                     media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                     sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    session.add(row)
    session.flush()
    return row
