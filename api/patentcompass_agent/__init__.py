"""Portable research instructions shared by hosted agents and legacy callers."""

from importlib.resources import files
from hashlib import sha256
import re


PROMPTS = {
    "planning": "patent-research-planning/references/planning.md",
    "qa": "patent-evidence-qa/references/qa.md",
    "qa-statistics": "patent-evidence-qa/references/statistics.md",
    "qa-budget": "patent-evidence-qa/references/budget.md",
    "semantic-extraction": "patent-semantic-analysis/references/extraction.md",
    "semantic-taxonomy": "patent-semantic-analysis/references/taxonomy.md",
    "report-summary": "patent-navigation-report/references/summary.md",
    "report-rewrite": "patent-result-revision/references/rewrite.md",
    "selection-rewrite": "patent-result-revision/references/selection.md",
    "chart-insight": "patent-result-revision/references/chart.md",
}


def read_prompt(name):
    return files(__package__).joinpath("skills", PROMPTS[name]).read_text(encoding="utf-8").strip()


# Identical extracted prompts retain compatibility with existing semantic caches.
_LEGACY_PROMPTS = {
    "semantic-extraction": "ec3b09c5000c228828e6261bb99b94ec9e075637aad7ad5bdad7e5b469e638ec",
    "semantic-taxonomy": "bd15137751aafbb2efcd6fc96cf9ee0c157a248bc0e4220987266c0df9c094ef",
    "report-summary": "42faba2eef3068a4099abe540aa110c634f14191180bbf6ea33a444a6b0c7bd4",
    "report-rewrite": "32f8e12e0457031faaa1c7d65af6c9093eda82c2c5f862fcc3f347989ad87801",
}


def prompt_version(name, legacy_version):
    digest = sha256(read_prompt(name).encode("utf-8")).hexdigest()
    return legacy_version if digest == _LEGACY_PROMPTS.get(name) else legacy_version + "-" + digest[:12]


def read_system_prompt():
    return files(__package__).joinpath("SYSTEM_PROMPT.md").read_text(encoding="utf-8").strip()


def list_skills():
    result = []
    for directory in sorted(files(__package__).joinpath("skills").iterdir(), key=lambda item: item.name):
        path = directory.joinpath("SKILL.md")
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        header = content.split("---", 2)[1]
        metadata = {key: re.search(r"(?m)^" + key + r":\s*(.+)$", header).group(1).strip().strip("\"'")
                    for key in ("name", "description")}
        if metadata["name"] != directory.name:
            raise ValueError("Skill name must match its directory")
        result.append(metadata)
    return result


def read_skill(name, reference=None):
    if name not in {item["name"] for item in list_skills()}:
        raise ValueError("Unknown skill")
    directory = files(__package__).joinpath("skills", name)
    references = sorted("references/" + path.name for path in directory.joinpath("references").iterdir()
                        if path.is_file() and path.name.endswith(".md"))
    if reference is not None and reference not in references:
        raise ValueError("Unknown skill reference")
    path = directory.joinpath(reference or "SKILL.md")
    return {"name": name, "resource": reference or "SKILL.md", "content": path.read_text(encoding="utf-8"),
            "references": references}
