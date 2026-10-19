"""Verify recovery of this project's Docker services against a saved HTTP acceptance run."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from uuid import UUID, uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def main(args):
    result_path = args.result
    if result_path is None:
        candidates = list((ROOT / "storage" / "acceptance").glob("*/result.json"))
        check(candidates, "Run scripts/smoke-test.py first")
        result_path = max(candidates, key=lambda path: path.stat().st_mtime)
    result_path = result_path.resolve()
    summary = json.loads(result_path.read_text(encoding="utf-8"))
    check(summary["status"] == "passed", "A passed HTTP acceptance result is required")
    project_id, report_id = summary["project_id"], summary["report_id"]
    v1 = summary["versions"]["v1"]
    faults = summary.setdefault("fault_checks", {})
    compose = ["docker", "compose", "-f", str(ROOT / "deploy" / "compose.yml")]

    def docker(*command):
        subprocess.run([*compose, *command], check=True, timeout=120)

    with httpx.Client(base_url=summary["base_url"], timeout=30) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        def create_analysis():
            response = client.post(f"/api/projects/{project_id}/runs", json={"action": "analyze"},
                                   headers={"Idempotency-Key": "fault-" + uuid4().hex})
            check(response.status_code == 202, f"Task was not accepted: {response.status_code} {response.text}")
            return response.json()["run_id"]

        def wait_run(run_id):
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                value = get(f"/api/runs/{run_id}")
                if value["status"] in {"succeeded", "failed", "cancelled"}:
                    check(value["status"] == "succeeded", f"Recovery run failed: {value.get('error')}")
                    check(value["result"]["application_count"] == 520, "Recovery run changed the sample count")
                    return value
                time.sleep(0.5)
            raise RuntimeError(f"Task {run_id} did not recover")

        def record_fault(name, callback):
            detail = faults[name] = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()}
            try:
                detail.update(callback())
                detail["status"] = "passed"
                print(f"{name}: passed", flush=True)
            except Exception as exc:
                detail.update(status="failed", error=str(exc))
                raise
            finally:
                detail["finished_at"] = datetime.now(timezone.utc).isoformat()
                result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        def redis_outage():
            docker("stop", "redis")
            try:
                run_id = create_analysis()
                time.sleep(3)
                pending = get(f"/api/runs/{run_id}")
                check(pending["status"] == "queued", "Task was not durably queued while Redis was stopped")
            finally:
                docker("up", "-d", "--wait", "redis")
            completed = wait_run(run_id)
            return {"run_id": run_id, "during_outage": pending["status"], "http_status": 202,
                    "after_recovery": completed["status"], "application_count": completed["result"]["application_count"]}

        def worker_outage():
            docker("stop", "worker")
            try:
                run_id = create_analysis()
                time.sleep(3)
                pending = get(f"/api/runs/{run_id}")
                check(pending["status"] == "queued", "Task was not queued while the worker was stopped")
            finally:
                docker("up", "-d", "worker")
            completed = wait_run(run_id)
            return {"run_id": run_id, "during_outage": pending["status"], "http_status": 202,
                    "after_recovery": completed["status"], "application_count": completed["result"]["application_count"]}

        def worker_interrupted():
            response = client.post(f"/api/reports/{report_id}/exports",
                                   json={"report_version_id": v1["id"], "format": "docx"},
                                   headers={"Idempotency-Key": "fault-" + uuid4().hex})
            check(response.status_code == 202, f"Export was not accepted: {response.text}")
            run_id = str(UUID(response.json()["run_id"]))
            deadline = time.monotonic() + min(args.timeout, 180)
            while time.monotonic() < deadline:
                active = get(f"/api/runs/{run_id}")
                checkpoint = next((step for step in active["steps"]
                                   if step["key"] == "export_snapshot" and step["status"] == "succeeded"), None)
                if active["status"] == "running" and checkpoint:
                    docker("kill", "-s", "SIGKILL", "worker")
                    break
                check(active["status"] not in {"succeeded", "failed", "cancelled"}, "Export finished before interruption could be tested")
                time.sleep(0.02)
            else:
                raise RuntimeError("Export did not reach the running checkpoint")
            try:
                stopped = get(f"/api/runs/{run_id}")
                check(stopped["status"] == "running", "Interruption did not leave an active lease to recover")
            finally:
                docker("up", "-d", "worker")
            print(f"worker-interrupted: killed run {run_id} after checkpoint; waiting for lease recovery", flush=True)
            progress = time.monotonic()
            while time.monotonic() < deadline:
                completed = get(f"/api/runs/{run_id}")
                if completed["status"] in {"succeeded", "failed", "cancelled"}:
                    break
                if time.monotonic() - progress >= 45:
                    print(f"worker-interrupted: status={completed['status']} attempt={completed['attempt']}; lease recovery pending", flush=True)
                    progress = time.monotonic()
                time.sleep(0.5)
            else:
                raise RuntimeError("Interrupted export did not recover within 180 seconds")
            check(completed["status"] == "succeeded" and completed["attempt"] >= 2,
                  f"Interrupted run did not complete on a second attempt: {completed}")
            saved = next(step for step in completed["steps"] if step["key"] == "export_snapshot")
            check(saved["id"] == checkpoint["id"] and saved["finished_at"] == checkpoint["finished_at"], "Completed snapshot checkpoint was executed again")
            check(completed["result"]["report_version_id"] == v1["id"], "Recovered export changed report version")
            document = client.get(f"/api/files/{completed['result']['file_id']}")
            document.raise_for_status()
            output = result_path.parent / "patentcompass-report-v1-recovered.docx"
            output.write_bytes(document.content)
            counts_query = (
                "SELECT json_build_object('file_count',(SELECT count(*) FROM files WHERE run_id='" + run_id + "'),"
                "'step_count',(SELECT count(*) FROM run_steps WHERE run_id='" + run_id + "'),"
                "'distinct_step_count',(SELECT count(DISTINCT key) FROM run_steps WHERE run_id='" + run_id + "'));"
            )
            counted = subprocess.run([*compose, "exec", "-T", "postgres", "psql", "-U", "patentcompass", "-d", "patentcompass", "-tA", "-c", counts_query],
                                     capture_output=True, text=True, check=True, timeout=30)
            counts = json.loads(counted.stdout.strip())
            check(counts["file_count"] == 1 and counts["step_count"] == counts["distinct_step_count"], "Recovered run created duplicate files or steps")
            return {"run_id": run_id, "during_interruption": stopped["status"], "attempt": completed["attempt"],
                    "after_recovery": completed["status"], "checkpoint_id": checkpoint["id"],
                    "checkpoint_finished_at": checkpoint["finished_at"], "checkpoint_reused": True,
                    "file_id": completed["result"]["file_id"], "download_path": str(output), **counts}

        def postgres_recreation():
            before = get(f"/api/projects/{project_id}")
            docker("up", "-d", "--force-recreate", "--wait", "postgres")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                try:
                    response = client.get("/health/ready")
                    if response.status_code == 200 and response.json()["status"] == "ready":
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("API did not reconnect after PostgreSQL recreation")
            after = get(f"/api/projects/{project_id}")
            check(after["id"] == before["id"] and after["dataset_version_id"] == before["dataset_version_id"], "Project data did not survive recreation")
            check(after["current_report_version_id"] == before["current_report_version_id"], "Adopted report version changed")
            check(get(f"/api/reports/{report_id}/versions/{v1['id']}") == v1, "Frozen report did not survive recreation")
            count = get(f"/api/projects/{project_id}/patents?limit=1")["total"]
            check(count == 520, "Imported records did not survive recreation")
            document = client.get(f"/api/files/{summary['export']['file_id']}")
            document.raise_for_status()
            check(len(document.content) == summary["export"]["bytes"], "Stored Word file did not survive recreation")
            return {"project_id": project_id, "report_version_id": v1["id"], "application_count": count,
                    "api_reconnected": True, "download_bytes": len(document.content)}

        handlers = {"redis": redis_outage, "worker": worker_outage, "worker-interrupted": worker_interrupted, "postgres": postgres_recreation}
        for name in args.checks:
            record_fault(name, handlers[name])
        print(f"Updated acceptance result: {result_path}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, help="Saved result.json; defaults to the most recently written acceptance result")
    parser.add_argument("--checks", nargs="+", choices=["redis", "worker", "worker-interrupted", "postgres"], default=["redis", "worker", "postgres"])
    parser.add_argument("--timeout", type=float, default=240)
    main(parser.parse_args())
