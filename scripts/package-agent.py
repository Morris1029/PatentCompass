"""Package maintained instructions for Codex or WorkBuddy without global installation."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def package(output):
    root = Path(__file__).resolve().parents[1]
    source = root / "api" / "patentcompass_agent"
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.write(source / "SYSTEM_PROMPT.md", "SYSTEM_PROMPT.md")
        archive.write(source / "BUILTIN_RUNTIME.md", "BUILTIN_RUNTIME.md")
        for path in sorted((source / "skills").rglob("*.md")):
            archive.write(path, path.relative_to(source).as_posix())
        archive.write(root / "docs" / "portable-agent.md", "INTEGRATION.md")
        archive.writestr("codex/AGENTS.md", "# PatentCompass\n\nRead ../SYSTEM_PROMPT.md before patent research tasks.\n")
        archive.writestr("codex/config.toml", '[mcp_servers.patentcompass]\nurl = "http://127.0.0.1:8000/mcp/"\n')
        archive.writestr("workbuddy/mcp.json", '{"mcpServers":{"patentcompass":{"type":"streamableHttp","url":"http://127.0.0.1:8000/mcp/"}}}\n')
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "storage" / "patentcompass-agent.zip"))
    args = parser.parse_args()
    print(package(args.output))
