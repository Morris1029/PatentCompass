"""Resource access and idempotency shared by HTTP, tools and MCP."""

import hashlib
import json
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text

from app.config import get_settings
from app.errors import AppError
from app.models import (Conversation, Dataset, DatasetVersion, IdempotencyRequest, Message,
                        Project, Report, ReportVersion, ResearchVersion)


def public_value(value):
    if isinstance(value, dict):
        return {key: public_value(item) for key, item in value.items()
                if key not in {"_response_items", "_generation_protocol"}}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def record(row, exclude=()):
    return jsonable_encoder({column.key: getattr(row, column.key) for column in row.__table__.columns
                             if column.key not in exclude})


def owned(session, model, object_id, lock=False):
    try:
        object_id = str(UUID(str(object_id)))
    except (ValueError, TypeError, AttributeError):
        raise AppError(422, "INVALID_RESOURCE_ID", "资源标识无效")
    statement = select(model).where(model.id == object_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    row = session.scalar(statement)
    if not row:
        raise AppError(404, "NOT_FOUND", "记录不存在")
    if hasattr(row, "owner_id"):
        permitted = row.owner_id == get_settings().dev_owner_id
    elif isinstance(row, DatasetVersion):
        permitted = session.get(Dataset, row.dataset_id).owner_id == get_settings().dev_owner_id
    elif isinstance(row, (ReportVersion, ResearchVersion)):
        permitted = session.get(Project, session.get(Report, row.report_id).project_id).owner_id == get_settings().dev_owner_id
    elif isinstance(row, Message):
        permitted = session.get(Project, session.get(Conversation, row.conversation_id).project_id).owner_id == get_settings().dev_owner_id
    elif getattr(row, "project_id", None):
        permitted = session.get(Project, row.project_id).owner_id == get_settings().dev_owner_id
    else:
        permitted = False
    if not permitted:
        raise AppError(404, "NOT_FOUND", "记录不存在")
    return row


def idempotent(session, operation, key, payload, callback, status_code=200):
    if not key or len(key) > 200:
        raise AppError(422, "INVALID_IDEMPOTENCY_KEY", "幂等标识需要 1 到 200 字符")
    owner = get_settings().dev_owner_id
    digest = hashlib.sha256(json.dumps(jsonable_encoder(payload), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": owner + operation + key})
    previous = session.scalar(select(IdempotencyRequest).where(IdempotencyRequest.owner_id == owner,
                              IdempotencyRequest.operation == operation, IdempotencyRequest.client_key == key))
    if previous:
        if previous.request_hash != digest:
            raise AppError(409, "IDEMPOTENCY_CONFLICT", "同一幂等标识不能提交不同内容")
        return previous.response, previous.status_code
    result = jsonable_encoder(callback())
    session.add(IdempotencyRequest(owner_id=owner, operation=operation, client_key=key,
                                  request_hash=digest, response=result, status_code=status_code))
    return result, status_code
