"""Verify a completed full semantic run through public HTTP APIs and independent counters."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import httpx
import openpyxl


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=ROOT / "storage/acceptance/frontend-semantic-20261019")
    parser.add_argument("--start-cache-run", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(base_url=args.base_url, timeout=60)

    def get(path, **params):
        response = client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    run = get(f"/api/runs/{args.run_id}")
    (args.output / f"run-{args.run_id}.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    if run["status"] != "succeeded":
        print(json.dumps({"status": run["status"], "run_id": args.run_id, "error": run.get("error")}, ensure_ascii=False))
        return 2
    project_id, analysis_id = run["project_id"], run["result"]["analysis_id"]
    views = {view: get(f"/api/projects/{project_id}/analysis", analysis_id=analysis_id, view=view)
             for view in ("overview", "technology", "landscape", "opportunities", "roadmap", "geography")}
    patents, cursor = [], None
    while True:
        page = get(f"/api/projects/{project_id}/patents", analysis_id=analysis_id, limit=200, **({"cursor": cursor} if cursor else {}))
        patents.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    by_id = {patent["id"]: patent for patent in patents}
    summary = views["overview"]["summary"]
    source = ROOT / "database/patent-demo.xlsx"
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    rows = [row for row in list(workbook.active.iter_rows(values_only=True))[1:] if row[0]]
    workbook.close()
    split = lambda value: set(part.strip() for part in re.split(r"[;；\r\n]+", str(value or "")) if part.strip())
    applicants, ipc, years, country = Counter(), Counter(), Counter(), Counter()
    for row in rows:
        applicants.update(split(row[5]))
        ipc.update(split(row[4]))
        years.update([int(str(row[2])[:4])])
        country.update(split(row[11]))
    assert len(rows) == len(patents) == len(by_id) == summary["application_count"] == 520
    assert sum(len(split(row[1])) for row in rows) == summary["publication_count"] == 526
    assert sum(bool(row[12]) for row in rows) == summary["abstract_count"] == 520
    assert {row["name"]: row["count"] for row in summary["applicants"]} == applicants
    assert {row["code"]: row["count"] for row in summary["ipc"]} == ipc
    assert {row["year"]: row["count"] for row in summary["yearly"]} == years
    assert {row["country"]: row["count"] for row in summary["country"]} == country == {"CN": 520, "JP": 1, "TJ": 1}
    technology = views["technology"]["data"]["data"]
    memberships = technology["memberships"]
    coverage = technology["coverage"]
    assert len(memberships) == len({row["application_id"] for row in memberships}) == len(patents)
    assert coverage["processed_count"] == coverage["total_count"] == 520
    assert coverage["classified_count"] + coverage["review_count"] == 520
    assert coverage["review_count"] == sum(row["status"] == "review" for row in memberships)
    matrix, annual, quote_count = Counter(), Counter(), 0
    for row in memberships:
        patent = by_id[row["application_id"]]
        for evidence in row["assignment"]["evidence"]:
            assert evidence["quote"] in patent[evidence["field"]]
            quote_count += 1
        for theme_id in set(row["assignment"]["methods"]):
            annual[(int(patent["application_date"][:4]), theme_id)] += 1
            for applicant in set(patent["applicants"]):
                matrix[(applicant, theme_id)] += 1
    landscape = views["landscape"]["data"]["data"]
    assert {(row["applicant"], row["theme_id"]): row["count"] for row in landscape["subject_matrix"]} == matrix
    assert {(row["year"], row["theme_id"]): row["count"] for row in landscape["annual_theme"]} == annual
    opportunities = views["opportunities"]["data"]["data"]["items"]
    roadmap = views["roadmap"]["data"]["data"]["items"]
    assert [row["id"] for row in opportunities] == [row["id"] for row in roadmap]
    for direction in opportunities:
        assert direction["external_validation"]["status"] == "not_performed"
        assert direction["support_count"] == len(direction["supporting_application_ids"])
        assert set(direction["supporting_application_ids"]) <= set(by_id)
        assert direction["counterexample_search"]["examined_count"] == 520
    for chart_id in run["result"]["chart_ids"]:
        chart = get(f"/api/charts/{chart_id}")
        assert chart["provenance"]["scope_version_id"] == run["scope_version_id"]
        assert chart["data_sha256"] == hashlib.sha256(json.dumps(chart["data"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if chart["data"]["rows"]:
            value = chart["data"]["rows"][0]["value"]
            drilldown = get(f"/api/projects/{project_id}/patents", chart_id=chart_id,
                            chart_version_id=chart["version_id"], dimension_value=value, limit=1)
            assert drilldown["total"] == len(chart["selections"]["values"][value])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    result = {"status": "verified", "run_id": run["id"], "project_id": project_id, "analysis_id": analysis_id,
              "sample_sha256": original_hash, "application_count": 520, "publication_count": 526, "abstract_count": 520,
              "coverage": coverage, "verified_original_quotes": quote_count, "subject_matrix_cells": len(matrix),
              "annual_theme_cells": len(annual), "candidate_directions": len(opportunities),
              "country": dict(country), "chart_count": len(run["result"]["chart_ids"]),
              "checks": ["independent_xlsx_counters", "full_membership_coverage", "exact_original_quotes",
                         "joint_applicant_theme_counts", "annual_theme_counts", "stable_direction_roadmap_ids",
                         "chart_snapshot_hashes", "historical_chart_drilldown", "original_file_unchanged"]}
    if args.start_cache_run:
        response = client.post(f"/api/projects/{project_id}/runs", json={"action": "analyze", "semantic_mode": "full"})
        response.raise_for_status()
        result["cache_run"] = response.json()
    (args.output / f"verification-{args.run_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / f"views-{analysis_id}.json").write_text(json.dumps(views, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
