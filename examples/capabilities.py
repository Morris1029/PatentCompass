"""Run patent capabilities directly, without app, PostgreSQL, Redis, or a model."""

import argparse
import json
from pathlib import Path
import sys

# The repository keeps the reusable package beside the API application.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from patentcompass_core.parsing import parse_workbook
from patentcompass_core.retrieval import search_patents
from patentcompass_core.statistics import compute_statistics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, help="Optional CNIPA workbook; the original file is only read")
    parser.add_argument("--query", default="landing", help="Keyword query")
    options = parser.parse_args()
    if options.workbook:
        parsed = parse_workbook(options.workbook)
        if parsed["quality"]["status"] != "ready":
            print(json.dumps({"quality": parsed["quality"]}, ensure_ascii=False, indent=2))
            return 1
        records = parsed["applications"]
        source = str(options.workbook.resolve())
    else:
        # These two explicitly labelled examples are not real patent findings.
        records = [
            {"id": "example-landing", "application_number": "EXAMPLE-1", "application_date": "2024-01-01",
             "title": "Example camera landing", "abstract": "An example camera supports autonomous landing.",
             "applicants": ["Example A", "Example B"], "ipc_codes": ["EXAMPLE"], "countries": ["CN"]},
            {"id": "example-battery", "application_number": "EXAMPLE-2", "application_date": "2025-01-01",
             "title": "Example battery charging", "abstract": "An example battery charging method.",
             "applicants": ["Example A"], "ipc_codes": ["EXAMPLE"], "countries": ["CN"]},
        ]
        source = "synthetic examples; not real patents"
    analysis = compute_statistics(records)
    found = search_patents(records, {"q": options.query, "mode": "keywords"})
    result = {"source": source,
              "counts": {key: analysis["summary"][key] for key in ("application_count", "publication_count", "abstract_count")},
              "search": {"query": options.query, "candidate_count": found["candidate_count"],
                         "scope_application_count": found["scope_application_count"],
                         "examples": [{"application_number": row["application_number"], "title": row["title"]}
                                      for row in found["items"][:5]]}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
