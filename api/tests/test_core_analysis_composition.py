"""Classification results compose into the same report-ready semantic analysis."""

import base64
from copy import deepcopy
from io import BytesIO
import json
from uuid import UUID

from docx import Document
import pytest

from patentcompass_core.contracts import CapabilityError
from patentcompass_core.rendering import build_docx
from patentcompass_core.reports import build_report_content
from patentcompass_core.semantics import analysis_draft, classify_patents


def records():
    return [{"id": value, "application_number": "EXAMPLE-" + value, "application_date": "2024-01-01",
             "title": "Camera landing", "abstract": "A camera supports autonomous landing on a moving platform.",
             "applicants": ["A", "A", "B"], "ipc_codes": ["G01"], "countries": ["CN"],
             "source_sheet": "Example", "source_row": index + 2,
             "publications": [{"publication_number": "EXAMPLE-" + value + "A"}]}
            for index, value in enumerate(("a", "b"))]


def generate(messages):
    value = json.loads(messages[-1]["content"])
    if "records" in value:
        tags = {"elements": ("Camera", "camera"), "methods": ("Landing", "autonomous landing"),
                "scenarios": ("Platform", "moving platform")}
        value = {"records": [{"id": row["id"], **{dimension: [{"label": label, "quote": quote,
            "field": "abstract", "confidence": .9}] for dimension, (label, quote) in tags.items()}}
            for row in value["records"]]}
    else:
        dimension = next(iter(value))
        value = {dimension: [{"label": dimension + " theme", "members": [row["id"] for row in value[dimension]]}]}
    return {"content": json.dumps(value)}


def test_classification_composes_semantic_views_evidence_charts_report_and_word():
    patents = records()
    classification = classify_patents(patents, generate)
    original = deepcopy(classification)
    draft = analysis_draft(patents, classification)
    assert classification == original
    assert draft == analysis_draft(patents, classification)
    assert draft["summary"]["semantic"]["classified_count"] == 2
    assert draft["summary"]["publication_count"] == 2
    methods = draft["views"]["technology"]["data"]["taxonomy"]["methods"]
    assert methods[0]["count"] == 2
    matrix = draft["views"]["landscape"]["data"]["subject_matrix"]
    assert [(item["applicant"], item["count"]) for item in matrix] == [("A", 2), ("B", 2)]
    assert draft["views"]["opportunities"]["data"]["items"][0]["support_count"] == 2
    assert draft["views"]["roadmap"]["data"]["items"][0]["phases"]
    assert len(draft["charts"]) == 9
    chart_ids = {chart["id"] for chart in draft["charts"]}
    evidence_ids = {evidence["id"] for evidence in draft["evidence"]}
    for chart in draft["charts"]:
        UUID(chart["id"])
        UUID(chart["version_id"])
    for view in draft["views"].values():
        assert set(view["chart_ids"]) <= chart_ids
        assert set(view["evidence_ids"]) == evidence_ids
    evidence = draft["evidence"][0]["content"]
    assert evidence["source_row"] == 2 and evidence["publication_numbers"] == ["EXAMPLE-aA"]
    assert evidence["semantic_evidence"][0]["quote"] in patents[0]["abstract"]
    snapshot = {**draft, "project_name": "Example", "scope": {}, "sources": [],
                "dataset_version_ids": [], "scope_version_id": None,
                "representatives": [{"application_id": row["id"], **row,
                    "publication_numbers": [item["publication_number"] for item in row["publications"]],
                    "dataset_version_id": None} for row in patents]}
    content = build_report_content(snapshot)
    assert any(block.get("text") == "全量语义分类与技术结构" for block in content["blocks"])
    assert any(block.get("text") == "候选研究方向与风险关注" for block in content["blocks"])
    assert any(block.get("section") == "roadmap" for block in content["blocks"])
    chart_blocks = [block for block in content["blocks"] if block["type"] == "chart"]
    assert len(chart_blocks) == 6
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/s9sAAAAASUVORK5CYII=")
    rendered = []

    def renderer(option, path):
        rendered.append(option)
        path.write_bytes(png)

    exported = Document(BytesIO(build_docx(content, 1, render_chart_callable=renderer)))
    assert len(exported.inline_shapes) == len(chart_blocks) == len(rendered)
    assert "全量语义分类与技术结构" in [paragraph.text for paragraph in exported.paragraphs]


@pytest.mark.parametrize("change", [{"abstract": "Different original text"}, {"applicants": ["Other"]},
                                   {"application_date": "2025-01-01"}])
def test_draft_rejects_classification_from_changed_source_records(change):
    patents = records()
    classification = classify_patents(patents, generate)
    patents[0].update(change)
    with pytest.raises(CapabilityError) as error:
        analysis_draft(patents, classification)
    assert error.value.status_code == 422 and error.value.code == "CLASSIFICATION_SOURCE_MISMATCH"


@pytest.mark.parametrize("change", ["missing_source", "wrong_ids", "unknown_theme", "invalid_quote"])
def test_draft_rejects_missing_source_or_invalid_classification_references(change):
    patents = records()
    classification = classify_patents(patents, generate)
    if change == "missing_source":
        classification.pop("source")
    elif change == "wrong_ids":
        classification["assignments"]["other"] = classification["assignments"].pop("a")
    elif change == "unknown_theme":
        classification["assignments"]["a"]["assignment"]["methods"] = ["unknown"]
    else:
        classification["assignments"]["a"]["assignment"]["evidence"][0]["quote"] = "Invented original text"
    with pytest.raises(CapabilityError) as error:
        analysis_draft(patents, classification)
    assert error.value.code == "CLASSIFICATION_SOURCE_MISMATCH"


def test_draft_recomputes_counts_and_directions_from_checked_memberships():
    patents = records()
    classification = classify_patents(patents, generate)
    classification["coverage"]["classified_count"] = 999
    classification["directions"] = [{"title": "Invented"}]
    draft = analysis_draft(patents, classification)
    assert draft["summary"]["semantic"]["classified_count"] == 2
    assert draft["views"]["opportunities"]["data"]["items"][0]["support_count"] == 2
