"""Explicit, provenance-preserving supplementary patent snapshot imports."""

import copy
import hashlib
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import openpyxl
from sqlalchemy import func, select

from app.data_service import HEADERS, _date, _priorities, _text, split_values
from app.db import session_scope
from app.errors import AppError
from app.files import resolve_file
from app.jobs import WaitingForInput, emit_event
from app.models import (DatasetSource, DatasetVersion, ImportConflict, PatentApplication,
                        PatentPublication, StoredFile, new_id)

LIST_FIELDS = {"applicants", "inventors", "ipc_codes", "countries", "priorities"}
APPLICATION_FIELDS = ("application_date", "title", "abstract", "applicants", "inventors",
                      "ipc_codes", "countries", "priorities")


def parse_supplement(path: str | Path, existing_numbers: set[str]) -> dict:
    """Partial rows are allowed only for existing original application numbers."""
    rows, errors, sheets, seen = [], [], [], set()
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise AppError(422, "INVALID_WORKBOOK", "无法读取 Excel 文件") from exc
    try:
        for sheet in workbook:
            iterator = sheet.iter_rows(values_only=True)
            header = [_text(item) for item in next(iterator, [])]
            if not any(header):
                continue
            mapped = {HEADERS[name]: index for index, name in enumerate(header) if name in HEADERS}
            sheets.append({"name": sheet.title, "headers": header})
            if "application_number" not in mapped:
                errors.append({"sheet": sheet.title, "row": 1, "message": "补充文件必须包含申请号列"})
                continue
            for row_number, row in enumerate(iterator, 2):
                if not any(_text(value) for value in row):
                    continue
                raw = {header[index] or f"column_{index + 1}":
                       value.isoformat() if isinstance(value, (date, datetime)) else value
                       for index, value in enumerate(row) if index < len(header)}
                values = {key: row[index] if index < len(row) else None for key, index in mapped.items()}
                try:
                    number = re.sub(r"\s+", "", _text(values["application_number"])).upper()
                    if not number or number in seen:
                        raise ValueError("申请号为空或重复")
                    fields = {}
                    for name in ("title", "abstract"):
                        if _text(values.get(name)):
                            fields[name] = _text(values[name])
                    if _text(values.get("application_date")):
                        fields["application_date"] = _date(values["application_date"]).isoformat()
                    for name in LIST_FIELDS - {"priorities"}:
                        if _text(values.get(name)):
                            fields[name] = list(dict.fromkeys(split_values(values[name])))
                    if _text(values.get("priority_dates")) and not _text(values.get("priority_numbers")):
                        raise ValueError("补充优先权日期时必须同时提供优先权号")
                    if _text(values.get("priority_numbers")):
                        fields["priorities"] = _priorities(values.get("priority_numbers"), values.get("priority_dates"))
                    numbers = split_values(values.get("publication_numbers"))
                    dates = split_values(values.get("publication_dates"))
                    if dates and len(dates) != len(numbers):
                        raise ValueError("公开编号与公开日期数量不一致")
                    publications, public_seen = [], set()
                    for index, public_number in enumerate(numbers):
                        public_number = re.sub(r"\s+", "", public_number).upper()
                        if not re.fullmatch(r"[A-Z]{2}\d+[A-Z]\d?", public_number) or public_number in public_seen:
                            raise ValueError("公开编号无效或重复")
                        public_seen.add(public_number)
                        publications.append({"publication_number": public_number,
                                             "publication_date": _date(dates[index]).isoformat() if dates else None,
                                             "kind_code": re.search(r"([A-Z]\d?)$", public_number).group(1)})
                    if number not in existing_numbers:
                        if not all(fields.get(name) for name in ("application_date", "title", "abstract")):
                            raise ValueError("新增申请必须提供申请日、标题和摘要")
                        if not publications or any(not item["publication_date"] for item in publications):
                            raise ValueError("新增申请必须提供公开编号及公开日期")
                    rows.append({"application_number": number, "fields": fields, "publications": publications,
                                 "raw_record": raw, "source_sheet": sheet.title, "source_row": row_number})
                    seen.add(number)
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append({"sheet": sheet.title, "row": row_number, "message": str(exc), "raw_record": raw})
    finally:
        workbook.close()
    if not rows and not errors:
        errors.append({"message": "工作簿中没有补充记录"})
    return {"rows": rows, "errors": errors, "sheets": sheets}


def _json_value(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else copy.deepcopy(value)


def _lineage(application, base):
    raw = copy.deepcopy(application.raw_record)
    if isinstance(raw.get("_sources"), list):
        return raw["_sources"]
    return [{"file_id": base.source_file_id, "dataset_version_id": base.id,
             "sheet": application.source_sheet, "row": application.source_row, "raw_record": raw}]


def _quality(session, version, parsed):
    applications = session.scalars(select(PatentApplication).where(PatentApplication.dataset_version_id == version.id)).all()
    publications = session.scalars(select(PatentPublication).where(PatentPublication.dataset_version_id == version.id)).all()
    unresolved = session.scalar(select(func.count()).select_from(ImportConflict).where(
        ImportConflict.version_id == version.id, ImportConflict.resolution.is_(None)))
    return {"status": "waiting_input" if unresolved else "ready", "application_count": len(applications),
            "publication_count": len(publications), "abstract_count": sum(bool(row.abstract) for row in applications),
            "source_row_count": session.get(DatasetVersion, version.base_version_id).quality.get("source_row_count", 0) + len(parsed["rows"]),
            "supplement_row_count": len(parsed["rows"]), "base_dataset_version_id": version.base_version_id,
            "unresolved_conflict_count": unresolved, "errors": [], "sheets": parsed["sheets"],
            "publication_prefixes": dict(Counter(pub.publication_number[:2] for pub in publications)),
            "warnings": ["此版本为原样本与补充文件合并后的完整快照，仍不代表检索结果全集"],
            "unavailable_fields": ["claims", "full_text", "legal_status", "family", "detailed_address"]}


def resolve_conflicts(session, version_id: str, choices: list[dict]) -> dict:
    version = session.scalar(select(DatasetVersion).where(DatasetVersion.id == version_id).with_for_update())
    if not version or version.status != "waiting_input":
        raise AppError(409, "IMPORT_NOT_WAITING", "此数据版本当前没有待处理冲突")
    conflicts = session.scalars(select(ImportConflict).where(ImportConflict.version_id == version_id)
                               .with_for_update()).all()
    unresolved = {item.id: item for item in conflicts if item.resolution is None}
    if not isinstance(choices, list) or any(not isinstance(choice, dict) for choice in choices):
        raise AppError(422, "INVALID_CONFLICT_CHOICES", "请逐项选择冲突处理方式")
    choice_ids = [choice.get("conflict_id") for choice in choices]
    if len(set(choice_ids)) != len(choice_ids) or set(choice_ids) != set(unresolved):
        raise AppError(422, "INCOMPLETE_CONFLICT_CHOICES", "必须且只能处理此版本的全部未解决冲突")
    # Validate the complete request before applying any choice.
    selected = []
    for choice in choices:
        conflict = unresolved[choice["conflict_id"]]
        mode = choice.get("choice")
        if mode not in {"old", "new", "merge"}:
            raise AppError(422, "INVALID_CONFLICT_CHOICE", "冲突处理方式必须为 old、new 或 merge")
        if mode == "merge":
            if conflict.field not in LIST_FIELDS or not isinstance(conflict.old_value, list) or not isinstance(conflict.new_value, list):
                raise AppError(422, "INVALID_CONFLICT_MERGE", "只有多值字段支持合并")
            value = copy.deepcopy(conflict.old_value)
            value.extend(item for item in conflict.new_value if item not in value)
        else:
            value = copy.deepcopy(conflict.old_value if mode == "old" else conflict.new_value)
        selected.append((conflict, mode, value))
    for conflict, mode, value in selected:
        conflict.resolution, conflict.chosen_value = mode, value
    session.flush()
    return {"dataset_version_id": version_id, "resolved_count": len(selected), "unresolved_conflict_count": 0}


def merge_dataset(ctx) -> dict:
    version_id = ctx.payload["dataset_version_id"]

    def parse():
        with session_scope() as session:
            version = session.get(DatasetVersion, version_id)
            base = session.get(DatasetVersion, version.base_version_id) if version else None
            if not base or base.status != "ready":
                raise AppError(409, "BASE_DATASET_NOT_READY", "基础数据版本必须已完成质量检查")
            source = session.get(StoredFile, version.source_file_id)
            path = resolve_file(source.key)
            if hashlib.sha256(path.read_bytes()).hexdigest() != source.sha256:
                raise AppError(409, "SOURCE_FILE_CHANGED", "原始文件校验失败")
            existing_numbers = set(session.scalars(select(PatentApplication.application_number).where(
                PatentApplication.dataset_version_id == base.id)).all())
        return parse_supplement(path, existing_numbers)

    parsed = ctx.step("parse_merge_workbook", parse)
    if parsed["errors"]:
        with session_scope() as session:
            version = session.get(DatasetVersion, version_id)
            ctx.guard(session)
            version.status = "failed"
            version.quality = {"status": "failed", "errors": parsed["errors"], "sheets": parsed["sheets"]}
        raise AppError(422, "DATASET_QUALITY_FAILED", "补充文件包含异常，请查看质量说明", {"errors": parsed["errors"]})

    def prepare():
        with session_scope() as session:
            version = session.scalar(select(DatasetVersion).where(DatasetVersion.id == version_id).with_for_update())
            ctx.guard(session)
            if version.status in {"waiting_input", "ready"}:
                return {"dataset_version_id": version.id}
            base = session.get(DatasetVersion, version.base_version_id)
            old_apps = session.scalars(select(PatentApplication).where(PatentApplication.dataset_version_id == base.id)).all()
            old_publications = session.scalars(select(PatentPublication).where(PatentPublication.dataset_version_id == base.id)).all()
            public_owner = {pub.publication_number: pub.application_id for pub in old_publications}
            old_by_number = {app.application_number: app for app in old_apps}
            old_by_id = {app.id: app for app in old_apps}
            # Reject cross-application publication collisions before writing a snapshot.
            incoming_owners = {}
            for patch in parsed["rows"]:
                app = old_by_number.get(patch["application_number"])
                for publication in patch["publications"]:
                    number = publication["publication_number"]
                    if ((number in public_owner and (not app or public_owner[number] != app.id)) or
                            (number in incoming_owners and incoming_owners[number] != patch["application_number"])):
                        raise AppError(422, "PUBLICATION_APPLICATION_CONFLICT", "同一公开编号不能归属于不同申请", {"publication_number": number})
                    incoming_owners[number] = patch["application_number"]
                    if number not in public_owner and not publication["publication_date"]:
                        raise AppError(422, "PUBLICATION_DATE_REQUIRED", "新增公开编号必须提供公开日期", {"publication_number": number})
            new_apps, new_publications = {}, {}
            for old in old_apps:
                row = PatentApplication(id=new_id(), dataset_version_id=version.id, application_number=old.application_number,
                                        **{field: copy.deepcopy(getattr(old, field)) for field in APPLICATION_FIELDS},
                                        raw_record={**copy.deepcopy(old.raw_record), "_sources": _lineage(old, base)},
                                        source_sheet=old.source_sheet, source_row=old.source_row)
                session.add(row)
                new_apps[old.application_number] = row
            session.flush()
            for old in old_publications:
                old_app = old_by_id[old.application_id]
                new = PatentPublication(dataset_version_id=version.id, application_id=new_apps[old_app.application_number].id,
                                        publication_number=old.publication_number, publication_date=old.publication_date, kind_code=old.kind_code)
                session.add(new)
                new_publications[old.publication_number] = new
            for patch in parsed["rows"]:
                source_location = {"file_id": version.source_file_id, "sheet": patch["source_sheet"],
                                   "row": patch["source_row"], "raw_record": patch["raw_record"]}
                application = new_apps.get(patch["application_number"])
                if not application:
                    fields = {key: value for key, value in patch["fields"].items()}
                    fields["application_date"] = _date(fields["application_date"])
                    application = PatentApplication(id=new_id(), dataset_version_id=version.id,
                                                    application_number=patch["application_number"], **fields,
                                                    raw_record={**patch["raw_record"], "_sources": [source_location]},
                                                    source_sheet=patch["source_sheet"], source_row=patch["source_row"])
                    session.add(application)
                    new_apps[application.application_number] = application
                else:
                    previous_sources = application.raw_record["_sources"]
                    application.raw_record = {**application.raw_record, "_sources": [*previous_sources, source_location]}
                    for field, value in patch["fields"].items():
                        old_value = _json_value(getattr(application, field))
                        if old_value in (None, "", []):
                            setattr(application, field, _date(value) if field == "application_date" else value)
                        elif old_value != value:
                            session.add(ImportConflict(version_id=version.id, application_number=application.application_number,
                                                       field=field, old_value=old_value, new_value=value,
                                                       source={"old": previous_sources, "new": source_location}))
                session.flush()
                for publication in patch["publications"]:
                    old = new_publications.get(publication["publication_number"])
                    if old and publication["publication_date"] and old.publication_date.isoformat() != publication["publication_date"]:
                        session.add(ImportConflict(version_id=version.id, application_number=application.application_number,
                                                   field="publication_date:" + old.publication_number,
                                                   old_value=old.publication_date.isoformat(), new_value=publication["publication_date"],
                                                   source={"new": source_location, "base_version_id": base.id}))
                    elif not old:
                        new = PatentPublication(application_id=application.id, dataset_version_id=version.id,
                                                **{**publication, "publication_date": _date(publication["publication_date"])})
                        session.add(new)
                        new_publications[new.publication_number] = new
            sources = session.scalars(select(DatasetSource).where(DatasetSource.version_id == base.id)).all()
            inherited = {source.file_id: source.details for source in sources}
            inherited.setdefault(base.source_file_id, {"source_dataset_version_id": base.id})
            for file_id, details in inherited.items():
                session.add(DatasetSource(version_id=version.id, file_id=file_id, base_version_id=base.id, details=copy.deepcopy(details)))
            if version.source_file_id not in inherited:
                session.add(DatasetSource(version_id=version.id, file_id=version.source_file_id, base_version_id=base.id,
                                          details={"sheets": parsed["sheets"], "row_count": len(parsed["rows"])}))
            session.flush()
            version.quality = _quality(session, version, parsed)
            version.status = version.quality["status"]
            return {"dataset_version_id": version.id}

    ctx.step("prepare_merge", prepare)
    with session_scope() as session:
        version = session.scalar(select(DatasetVersion).where(DatasetVersion.id == version_id).with_for_update())
        run = ctx.guard(session)
        conflicts = session.scalars(select(ImportConflict).where(ImportConflict.version_id == version_id,
                                                                ImportConflict.resolution.is_(None))).all()
        if conflicts:
            request = {"id": new_id(), "kind": "import_conflicts", "dataset_version_id": version_id,
                       "prompt": "补充文件与当前数据存在差异，请逐项确认后继续", "conflict_count": len(conflicts)}
            run.status, run.input_request = "waiting_input", request
            run.lease_token, run.lease_expires_at = None, None
            emit_event(session, run.id, "input.required", request)
    if conflicts:
        raise WaitingForInput()

    def finalize():
        with session_scope() as session:
            version = session.scalar(select(DatasetVersion).where(DatasetVersion.id == version_id).with_for_update())
            ctx.guard(session)
            applications = {item.application_number: item for item in session.scalars(select(PatentApplication).where(
                PatentApplication.dataset_version_id == version.id)).all()}
            for conflict in session.scalars(select(ImportConflict).where(ImportConflict.version_id == version.id)).all():
                if conflict.field.startswith("publication_date:"):
                    publication = session.scalar(select(PatentPublication).where(PatentPublication.dataset_version_id == version.id,
                        PatentPublication.publication_number == conflict.field.split(":", 1)[1]))
                    publication.publication_date = _date(conflict.chosen_value)
                else:
                    value = conflict.chosen_value
                    setattr(applications[conflict.application_number], conflict.field,
                            _date(value) if conflict.field == "application_date" else copy.deepcopy(value))
            session.flush()
            version.quality = _quality(session, version, parsed)
            version.status = "ready"
            return {"dataset_version_id": version.id, **version.quality}

    result = ctx.step("finalize_merge", finalize)
    ctx.emit("dataset.ready", result)
    return result
