"""Standalone CNIPA spreadsheet parsing with source-row provenance."""

import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import openpyxl

from .contracts import CapabilityError


HEADERS = {
    "申请号": "application_number", "公开（公告）号": "publication_numbers", "申请日": "application_date",
    "公开（公告）日": "publication_dates", "IPC分类号": "ipc_codes", "申请（专利权）人": "applicants",
    "发明人": "inventors", "发明名称": "title", "优先权号": "priority_numbers", "优先权日": "priority_dates",
    "文献类型": "document_type", "申请人所在国（省）": "countries", "摘要": "abstract",
}
REQUIRED = {"application_number", "publication_numbers", "application_date", "publication_dates", "title", "abstract"}


def _text(value) -> str:
    return "" if value is None else str(value).strip()



def split_values(value) -> list[str]:
    return [part.strip() for part in re.split(r"[;；\n\r]+", _text(value)) if part.strip()]



def _date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for pattern in ("%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"无效日期: {text}")



def _priorities(number_value, date_value) -> list[dict]:
    numbers = split_values(number_value)
    dates = [_date(value).isoformat() for value in split_values(date_value)]
    priorities = []
    for index, value in enumerate(numbers):
        # CNIPA exports a date per number inside the number cell, while the date
        # column can contain only distinct dates; positional zipping would lose rows.
        match = re.fullmatch(r"(.+?)\s+(\d{8})", value)
        embedded_date = _date(match.group(2)).isoformat() if match else None
        if embedded_date and dates and embedded_date not in dates:
            raise ValueError("优先权号内日期与优先权日期列不一致")
        if embedded_date:
            priority_date = embedded_date
        elif len(dates) == len(numbers):
            priority_date = dates[index]
        elif len(dates) == 1:
            priority_date = dates[0]
        elif dates:
            raise ValueError("无法确定优先权号与日期的对应关系")
        else:
            priority_date = None
        priorities.append({"number": match.group(1) if match else value, "date": priority_date, "raw": value})
    return priorities



def parse_workbook(path: str | Path) -> dict:
    """Parse all non-empty rows. Any invalid row makes the version fail, never silently disappear."""
    applications, errors, sheets, missing_fields = [], [], [], Counter()
    seen_applications, seen_publications = set(), set()
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise CapabilityError(422, "INVALID_WORKBOOK", "无法读取 Excel 文件") from exc
    try:
        for sheet in workbook:
            iterator = sheet.iter_rows(values_only=True)
            header = [_text(item) for item in next(iterator, [])]
            if not any(header):
                continue
            mapped = {HEADERS[name]: index for index, name in enumerate(header) if name in HEADERS}
            missing = sorted(REQUIRED - mapped.keys())
            sheets.append({"name": sheet.title, "headers": header, "missing_columns": missing})
            if missing:
                errors.append({"sheet": sheet.title, "row": 1, "code": "MISSING_COLUMNS", "fields": missing})
            for row_number, row in enumerate(iterator, 2):
                if not any(_text(value) for value in row):
                    continue
                raw = {header[index] or f"column_{index + 1}":
                       value.isoformat() if isinstance(value, (date, datetime)) else value
                       for index, value in enumerate(row) if index < len(header)}
                values = {key: row[index] if index < len(row) else None for key, index in mapped.items()}
                for field in HEADERS.values():
                    if not _text(values.get(field)):
                        missing_fields[field] += 1
                try:
                    if missing:
                        raise ValueError("缺少必要列")
                    number = re.sub(r"\s+", "", _text(values["application_number"])).upper()
                    if not number or number in seen_applications:
                        raise ValueError("申请号为空或重复")
                    publications = split_values(values["publication_numbers"])
                    publication_dates = split_values(values["publication_dates"])
                    if not publications or len(publications) != len(publication_dates):
                        raise ValueError("公开编号与公开日期数量不一致")
                    publication_rows = []
                    local_publications = set()
                    for public_number, public_date in zip(publications, publication_dates):
                        public_number = re.sub(r"\s+", "", public_number).upper()
                        match = re.fullmatch(r"[A-Z]{2}\d+[A-Z]\d?", public_number)
                        if not match or public_number in seen_publications or public_number in local_publications:
                            raise ValueError("公开编号无效或重复")
                        local_publications.add(public_number)
                        kind = re.search(r"([A-Z]\d?)$", public_number).group(1)
                        publication_rows.append({"publication_number": public_number,
                                                 "publication_date": _date(public_date).isoformat(), "kind_code": kind})
                    if not _text(values["title"]) or not _text(values["abstract"]):
                        raise ValueError("标题或摘要为空")
                    priorities = _priorities(values.get("priority_numbers"), values.get("priority_dates"))
                    application = {"application_number": number, "application_date": _date(values["application_date"]).isoformat(),
                                   "title": _text(values["title"]), "abstract": _text(values["abstract"]),
                                   "applicants": split_values(values.get("applicants")), "inventors": split_values(values.get("inventors")),
                                   "ipc_codes": list(dict.fromkeys(split_values(values.get("ipc_codes")))),
                                   "countries": split_values(values.get("countries")), "priorities": priorities,
                                   "raw_record": raw, "source_sheet": sheet.title, "source_row": row_number,
                                   "publications": publication_rows}
                    applications.append(application)
                    seen_applications.add(number)
                    seen_publications.update(local_publications)
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append({"sheet": sheet.title, "row": row_number, "code": "INVALID_ROW", "message": str(exc), "raw_record": raw})
    finally:
        workbook.close()
    if not sheets:
        errors.append({"code": "EMPTY_WORKBOOK", "message": "工作簿中没有有效工作表"})
    if not applications and not errors:
        errors.append({"code": "NO_RECORDS", "message": "工作簿中没有专利记录"})
    quality = {"status": "failed" if errors else "ready", "application_count": len(applications),
               "publication_count": len(seen_publications), "abstract_count": sum(bool(a["abstract"]) for a in applications),
               "source_row_count": len(applications) + sum(e.get("code") == "INVALID_ROW" for e in errors),
               "missing_fields": dict(missing_fields), "errors": errors, "sheets": sheets,
               "warnings": ["当前数据为部分导出样本，统计不代表检索结果全集", "摘要为申请记录级文本，不为每个公开版本重复补造摘要",
                            "优先权号内嵌日期逐项保留，优先权日期列可能仅列出不重复日期；多申请人和多国家字段保留全部值"],
               "unavailable_fields": ["claims", "full_text", "legal_status", "family", "detailed_address"],
               "publication_prefixes": dict(Counter(pub["publication_number"][:2] for a in applications for pub in a["publications"]))}
    return {"applications": applications, "quality": quality}



LIST_FIELDS = {"applicants", "inventors", "ipc_codes", "countries", "priorities"}
APPLICATION_FIELDS = ("application_date", "title", "abstract", "applicants", "inventors",
                      "ipc_codes", "countries", "priorities")


def parse_supplement(path: str | Path, existing_numbers: set[str]) -> dict:
    """Partial rows are allowed only for existing original application numbers."""
    rows, errors, sheets, seen = [], [], [], set()
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise CapabilityError(422, "INVALID_WORKBOOK", "无法读取 Excel 文件") from exc
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

