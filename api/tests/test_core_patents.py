"""Independent patent components retain the application's counting and evidence rules."""

import json
import subprocess
import sys
from pathlib import Path

import openpyxl
import pytest
from docx import Document

from patentcompass_core.contracts import CapabilityError
from patentcompass_core.documents import parse_docx
from patentcompass_core.parsing import HEADERS, parse_supplement, parse_workbook
from patentcompass_core.retrieval import keyword_groups, search_patents
from patentcompass_core.semantics import classify_patents, validate_extraction
from patentcompass_core.statistics import compute_statistics


def patent(record_id="a", **changes):
    return {"id": record_id, "application_number": "CN" + record_id, "application_date": "2024-02-01",
            "title": "Autonomous landing", "abstract": "A camera supports autonomous landing on a moving platform.",
            "applicants": ["A", "A", "B"], "ipc_codes": ["G01", "G01", "G02"], "countries": ["CN", "US"],
            **changes}


def test_core_imports_and_runs_without_application_or_database(tmp_path):
    package_root = str(Path(__file__).resolve().parents[1])
    script = """
import importlib.abc, json, sys
sys.path.insert(0, sys.argv[1])
class BlockApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'sqlalchemy', 'celery', 'fastapi', 'redis'}:
            raise AssertionError('Core imported application infrastructure: ' + fullname)
sys.meta_path.insert(0, BlockApplication())
from patentcompass_core import documents, parsing, retrieval, semantics, statistics
result = statistics.compute_statistics([{'id':'a','application_date':'2024-01-01','title':'landing'}])
assert result['summary']['application_count'] == 1
assert retrieval.search_patents([{'id':'a','title':'landing'}], {'q':'landing'})['candidate_count'] == 1
print(json.dumps(result))
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, package_root], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["summary"]["yearly"] == [{"year": 2024, "count": 1}]


def test_statistics_count_applications_and_distinct_labels_with_original_sources():
    records = [patent(), patent("b", applicants=["A"], ipc_codes=["G01"], countries=["CN"], application_date="2023-01-01")]
    publications = [{"application_id": "a", "publication_number": "CN1A"},
                    {"application_id": "a", "publication_number": "CN1B"}]
    result = compute_statistics(records, publications, [{"quality": {"status": "ready"}, "source_metadata": {"region": "CN"}}])
    assert result["summary"]["application_count"] == 2
    assert result["summary"]["publication_count"] == 2
    assert result["summary"]["applicants"] == [{"name": "A", "count": 2}, {"name": "B", "count": 1}]
    assert result["summary"]["ipc"] == [{"code": "G01", "count": 2}, {"code": "G02", "count": 1}]
    assert result["selections"]["country"] == {"CN": ["a", "b"], "US": ["a"]}
    assert result["representative"][0]["publication_numbers"] == ["CN1A", "CN1B"]
    json.dumps(result)


def test_spreadsheet_and_partial_supplement_parsing_are_independent(tmp_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    headers = list(HEADERS)
    sheet.append(headers)
    values = {"申请号": "CN100", "公开（公告）号": "CN100A", "申请日": "2024.01.01",
              "公开（公告）日": "2024.06.01", "发明名称": "Landing", "摘要": "Landing with camera",
              "申请（专利权）人": "A；B"}
    sheet.append([values.get(header) for header in headers])
    path = tmp_path / "patents.xlsx"
    workbook.save(path)
    result = parse_workbook(path)
    assert result["quality"]["status"] == "ready"
    assert result["applications"][0]["applicants"] == ["A", "B"]
    assert result["applications"][0]["source_row"] == 2
    sheet.delete_rows(1, sheet.max_row)
    sheet.append(["申请号", "发明名称"])
    sheet.append(["CN100", "Updated title"])
    workbook.save(path)
    assert parse_supplement(path, {"CN100"})["rows"][0]["fields"] == {"title": "Updated title"}
    assert parse_supplement(path, set())["errors"]


def test_word_parser_retains_paragraph_and_merged_cell_locations(tmp_path):
    document = Document()
    document.add_paragraph("Research purpose")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Merged evidence"
    path = tmp_path / "source.docx"
    document.save(path)
    blocks = parse_docx(path)
    assert [block["text"] for block in blocks] == ["Research purpose", "Merged evidence"]
    assert blocks[0]["location"] == {"paragraph": 1}
    assert blocks[1]["location"] == {"table": 1, "row": 1, "cell": 1}


def test_keyword_and_scope_filters_keep_and_or_semantics_and_counts():
    records = [patent(), patent("b", title="Drone battery", abstract="battery charging"),
               patent("c", application_date="2022-01-01")]
    assert keyword_groups("自主 降落") == [["自主", "autonomous", "automatic"], ["降落", "landing", "land"]]
    result = search_patents(records, {"q": "自主 降落", "applicant": "A"}, scope_filters={"start": 2024})
    assert [row["id"] for row in result["items"]] == ["a"]
    assert result["scope_application_count"] == 2 and result["candidate_count"] == 1
    assert search_patents(records, {"q": "land%"})["candidate_count"] == 2


def test_semantic_and_hybrid_ranking_keep_vector_and_keyword_ranks_separate():
    records = [patent("a", title="Battery", abstract="battery"), patent("b"), patent("c", title="Other", abstract="other")]
    embeddings = {"a": [1., 0.], "b": [.5, .5], "c": [0., 1.]}
    semantic = search_patents(records, {"mode": "semantic", "q": "landing", "candidate_limit": 2},
                              embeddings=embeddings, query_vector=[1., 0.])
    assert [row["id"] for row in semantic["items"]] == ["a", "b"]
    hybrid = search_patents(records, {"mode": "hybrid", "q": "landing", "candidate_limit": 2},
                            embeddings=embeddings, query_vector=[1., 0.])
    assert [row["id"] for row in hybrid["items"]] == ["b", "a"]
    assert hybrid["scores"]["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert hybrid["scope_application_count"] == 3 and hybrid["candidate_count"] == 2


@pytest.mark.parametrize("embeddings,query,code", [({}, [1., 0.], "EMBEDDING_INDEX_NOT_READY"),
                          ({"a": [1.]}, [1., 0.], "EMBEDDING_DIMENSION_MISMATCH"),
                          ({"a": [0., 0.]}, [1., 0.], "INVALID_EMBEDDING")])
def test_invalid_vector_inputs_fail_explicitly(embeddings, query, code):
    with pytest.raises(CapabilityError) as error:
        search_patents([patent()], {"mode": "semantic", "q": "landing"}, embeddings=embeddings, query_vector=query)
    assert error.value.code == code


def semantic_model(messages):
    payload = json.loads(messages[-1]["content"])
    if "records" in payload:
        labels = {"elements": ("Camera", "camera"), "methods": ("Landing", "autonomous landing"),
                  "scenarios": ("Moving platform", "moving platform")}
        records = [{"id": row["id"], **{dimension: [{"label": label, "field": "abstract", "quote": quote, "confidence": .9}]
                    for dimension, (label, quote) in labels.items()}} for row in payload["records"]]
        return {"content": json.dumps({"records": records})}
    dimension = next(iter(payload))
    return {"content": json.dumps({dimension: [{"label": "Theme", "members": [row["id"] for row in payload[dimension]]}]})}


def test_classification_reuses_evidence_rules_and_reports_local_direction_boundaries():
    result = classify_patents([patent(), patent("b")], semantic_model)
    assert result["coverage"]["classified_count"] == 2
    assert result["coverage"]["review_count"] == 0
    assert result["assignments"]["a"]["assignment"]["evidence"][0]["quote"] == "camera"
    assert result["directions"][0]["support_count"] == 2
    assert result["directions"][0]["external_validation"]["status"] == "not_performed"
    json.dumps(result)


def test_unverified_or_low_confidence_quotes_do_not_enter_theme_counts():
    row = {"elements": [{"label": "Invented", "quote": "not in original", "confidence": .9}],
           "methods": [{"label": "Uncertain", "quote": "autonomous landing", "confidence": .5}], "scenarios": []}
    extraction = validate_extraction(patent(), row)
    assert extraction["elements"] == [] and extraction["methods"] == []
    assert "elements:unverified_evidence" in extraction["review_reasons"]
    assert "methods:low_confidence" in extraction["review_reasons"]


def test_classification_repairs_only_missing_records_and_preserves_completed_work():
    calls = []

    def partial(messages):
        payload = json.loads(messages[-1]["content"])
        if "records" in payload:
            calls.append([row["id"] for row in payload["records"]])
            if len(calls) == 1:
                payload["records"] = payload["records"][:1]
                messages = [*messages[:-1], {"role": "user", "content": json.dumps(payload)}]
        return semantic_model(messages)

    result = classify_patents([patent(), patent("b")], partial)
    assert calls == [["p0", "p1"], ["p1"]]
    assert result["coverage"]["classified_count"] == 2
