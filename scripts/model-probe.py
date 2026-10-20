"""Small real-provider connectivity probe; never prints API keys or vectors."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
os.chdir(ROOT / "api")

from app.config import get_settings
from app.model_provider import _post, embed


def run(kind):
    settings = get_settings()
    started = time.monotonic()
    prefix = "llm" if kind == "chat" else "embedding"
    result = {"model": getattr(settings, f"{prefix}_model"),
              "host": urlsplit(getattr(settings, f"{prefix}_base_url")).hostname}
    try:
        if kind == "chat":
            response = _post("chat/completions", {"messages": [{"role": "user", "content": "只回复 API_OK，不要附加解释。"}]})
            content = response["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Provider returned no answer")
            result.update(status="passed", response=content[:100], usage=response.get("usage"))
        else:
            vectors = embed(["无人机自主降落", "Unmanned aerial vehicle autonomous landing"])
            result.update(status="passed", count=len(vectors), dimensions=[len(vector) for vector in vectors])
    except Exception as error:
        result.update(status="failed", code=getattr(error, "code", type(error).__name__))
        cause = error.__cause__
        response = getattr(cause, "response", None)
        if response is not None:
            result["http_status"] = response.status_code
            try:
                body = response.json()
                detail = body.get("error", body)
                message = str(detail.get("message", ""))
                for secret in (settings.llm_api_key, settings.embedding_api_key):
                    if secret:
                        message = message.replace(secret, "[REDACTED]")
                result["provider_code"] = str(detail.get("code", ""))[:80]
                result["provider_message"] = message[:300]
            except (ValueError, AttributeError):
                pass
    result["seconds"] = round(time.monotonic() - started, 2)
    return kind, result


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(run, ["chat", "embedding"]))
    output = ROOT / "storage" / "acceptance" / ("model-connectivity-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir(parents=True)
    (output / "probe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("Probe result:", output / "probe.json")
    sys.exit(0 if all(value["status"] == "passed" for value in results.values()) else 1)
