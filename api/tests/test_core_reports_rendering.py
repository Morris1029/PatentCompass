import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys

from docx import Document
import pytest

from patentcompass_core import charts, rendering, reports
from patentcompass_core.contracts import CapabilityError


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def snapshot():
    return {"project_name": "Standalone research", "analysis_id": "analysis-frozen", "scope": {"industry": "无人机"},
            "scope_version_id": "scope-frozen", "dataset_version_ids": ["dataset-frozen"],
            "summary": {"application_count": 2, "publication_count": 3, "abstract_count": 2,
                        "yearly": [{"year": 2024, "count": 2}], "applicants": [], "ipc": []},
            "sources": [], "charts": [], "evidence": [], "representatives": [], "views": {}}


def test_report_core_imports_without_application_or_storage(tmp_path):
    code = """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class NoApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'sqlalchemy', 'celery'}:
            raise AssertionError('Unexpected infrastructure import: ' + fullname)
sys.meta_path.insert(0, NoApplication())
from patentcompass_core import reports, charts, rendering
assert charts.build_chart_options({'title': 'Count', 'rows': [], 'kind': 'yearly'})['series'][0]['data'] == []
"""
    subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1])],
                   cwd=tmp_path, check=True, capture_output=True, text=True)


def test_report_build_and_revision_use_only_detached_explicit_facts():
    source = snapshot()
    content = reports.build_report_content(source)
    before = deepcopy(content)
    paragraph = next(block for block in content["blocks"] if block["type"] == "paragraph")
    revised = reports.revise_report_content(content, [
        {"op": "set_title", "text": "Standalone revised"},
        {"op": "replace_text", "block_id": paragraph["id"], "text": "Two frozen records"},
    ])
    source["summary"]["application_count"] = 999
    assert revised["title"] == "Standalone revised"
    assert content == before
    assert revised["source"]["summary"]["application_count"] == 2
    assert revised["source"]["dataset_version_ids"] == ["dataset-frozen"]
    assert reports.build_report_content(snapshot())["blocks"] == content["blocks"]
    with pytest.raises(CapabilityError, match="指定正文块不存在"):
        reports.revise_report_content(content, [{"op": "set_title", "text": "Must roll back"},
                                                {"op": "remove_block", "block_id": "missing"}])
    assert content == before


def test_report_revision_resolves_explicit_frozen_chart_and_evidence():
    content = reports.build_report_content(snapshot())
    chart = {"id": "chart", "version_id": "chart-v1", "kind": "yearly", "data": {"title": "Frozen year"},
             "option": {"series": [{"type": "bar", "data": [2]}]},
             "evidence_snapshot": [{"id": "evidence", "kind": "statistics", "content": {"count": 2}}]}
    revised = reports.revise_report_content(content, [
        {"op": "insert_chart", "chart_id": "chart", "chart_version_id": "chart-v1"}], charts=[chart])
    saved = next(block for block in revised["blocks"] if block["type"] == "chart")
    assert "evidence_snapshot" not in saved["chart"]
    assert revised["evidence_snapshot"] == chart["evidence_snapshot"]
    chart["option"]["series"][0]["data"][0] = 999
    assert saved["chart"]["option"]["series"][0]["data"] == [2]
    with pytest.raises(CapabilityError) as failure:
        reports.revise_report_content(content, [{"op": "insert_chart", "chart_id": "chart",
                                                 "chart_version_id": "another-version"}], charts=[chart])
    assert failure.value.code == "CHART_NOT_FOUND"


def test_natural_language_revision_requires_an_explicit_generator():
    content = reports.build_report_content(snapshot())
    block = next(item for item in content["blocks"] if item["type"] == "paragraph")
    with pytest.raises(CapabilityError) as failure:
        reports.revise_report_content(content, instruction="简化", block_id=block["id"])
    assert failure.value.code == "MODEL_NOT_CONFIGURED"
    seen = []

    def generate(messages):
        seen.append(json.loads(messages[-1]["content"]))
        return {"content": "仅覆盖两条样本。"}

    result = reports.revise_report_content(content, instruction="简化", block_id=block["id"],
                                            generate_callable=generate)
    assert seen[0]["source"]["summary"]["application_count"] == 2
    assert next(item for item in result["blocks"] if item["id"] == block["id"])["text"] == "仅覆盖两条样本。"
    assert next(item for item in content["blocks"] if item["id"] == block["id"])["text"] == block["text"]


def test_heatmap_uses_exact_record_memberships_and_style_cannot_edit_data():
    records = [{"id": "p1", "application_date": "2024-01-01", "applicants": ["A", "A", "B"]},
               {"id": "p2", "application_date": "2025-01-01", "applicants": ["B"]}]
    assignments = {"p1": {"assignment": {"methods": ["m1"]}}, "p2": {"assignment": {"methods": ["m2"]}}}
    result = charts.heatmap_snapshot("subject_matrix", "Methods", records, assignments,
                                     [{"id": "m1", "label": "Method 1"}, {"id": "m2", "label": "Method 2"}])
    cells = {(row["applicant"], row["theme_id"]): row["count"] for row in result["data"]["rows"]}
    assert cells == {("A", "m1"): 1, ("B", "m1"): 1, ("B", "m2"): 1}
    changed = charts.apply_chart_style(result["option"], {"visualMap": {"inRange": {"color": ["#fff", "#000"]}}})
    assert changed["series"] == result["option"]["series"]
    with pytest.raises(CapabilityError) as failure:
        charts.apply_chart_style(result["option"], {"series": [{"data": [999]}]})
    assert failure.value.code == "INVALID_CHART_STYLE"


def test_docx_needs_no_application_or_renderer_for_text_and_uses_explicit_chart_renderer():
    standalone = Document(BytesIO(rendering.build_docx({"title": "Independent note", "blocks": [
        {"id": "p", "type": "paragraph", "text": "No project or analysis record is required."}]}, 1)))
    assert standalone.paragraphs[1].text == "报告版本 1"
    content = reports.build_report_content(snapshot())
    data = rendering.build_docx(content, 7)
    document = Document(BytesIO(data))
    assert document.paragraphs[0].text == content["title"]
    assert "报告版本 7" in document.paragraphs[1].text
    assert len(document.tables) == 3
    content["blocks"].append({"id": "chart", "type": "chart", "chart": {"option": {"frozen": True}},
                               "layout": {"width_percent": 50, "alignment": "right"}})
    with pytest.raises(CapabilityError) as failure:
        rendering.build_docx(content, 7)
    assert failure.value.code == "CHART_RENDERER_UNAVAILABLE"
    seen = []

    def render(option, path):
        seen.append(deepcopy(option))
        path.write_bytes(PNG)

    document = Document(BytesIO(rendering.build_docx(content, 7, render_chart_callable=render)))
    assert seen == [{"frozen": True}]
    assert len(document.inline_shapes) == 1
    assert round(document.inline_shapes[0].width.inches, 2) == 3.25


def test_chart_renderer_returns_bytes_and_honors_explicit_paths_and_failures(tmp_path, monkeypatch):
    script = tmp_path / "renderer.mjs"
    script.write_text("// controlled renderer", encoding="utf-8")
    config = rendering.RendererConfig(node_binary="explicit-node", script_path=script, timeout_seconds=9)
    seen = []

    def run(command, **kwargs):
        seen.append((command[:2], kwargs["timeout"], json.loads(Path(command[3]).read_text(encoding="utf-8"))))
        Path(command[5]).write_bytes(PNG)

    monkeypatch.setattr(rendering.subprocess, "run", run)
    assert rendering.render_chart({"frozen": True}, renderer=config) == PNG
    target = tmp_path / "saved.png"
    assert rendering.render_chart({"frozen": True}, target, renderer=config) == target.read_bytes() == PNG
    assert seen == [(["explicit-node", str(script)], 9, {"frozen": True})] * 2

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("explicit-node", 9)

    monkeypatch.setattr(rendering.subprocess, "run", fail)
    with pytest.raises(CapabilityError) as failure:
        rendering.render_chart({}, renderer=config)
    assert failure.value.code == "CHART_RENDER_FAILED"


def test_application_docx_wrapper_keeps_renderer_monkeypatch_seam(monkeypatch):
    from app import report_service

    content = reports.build_report_content(snapshot())
    content["blocks"].append({"id": "chart", "type": "chart", "chart": {"option": {"fixed": 1}}})
    seen = []

    def render(option, path):
        seen.append(option)
        path.write_bytes(PNG)

    monkeypatch.setattr(report_service, "render_chart", render)
    document = Document(BytesIO(report_service.build_docx(content, 1)))
    assert seen == [{"fixed": 1}]
    assert len(document.inline_shapes) == 1
