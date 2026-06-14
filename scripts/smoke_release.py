#!/usr/bin/env python3
"""Install and verify the built Creative Tagger MCP release artifact.

This catches packaging mistakes that source-only tests miss: missing package
files, wrong version metadata, broken console entry points, or a stale tool
surface in the wheel that would be uploaded to PyPI.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
DIST = ROOT / "dist"

EXPECTED_TOOLS = {
    "analyze_creative",
    "get_taxonomy",
    "list_library",
    "get_library_patterns",
    "get_analysis",
    "recommend",
    "analyze_gaps",
    "get_brand_context",
    "set_brand_context",
    "get_brand_taxonomy",
    "set_brand_taxonomy_value",
    "delete_brand_taxonomy_value",
    "set_brand_entity",
    "delete_brand_entity",
    "get_naming_variables",
    "list_naming_templates",
    "save_naming_template",
    "delete_naming_template",
    "preview_naming_template",
    "get_meta_status",
    "sync_meta_performance",
    "get_meta_performance_summary",
    "get_taxonomy_performance",
    "get_prebuilt_reports",
    "get_creative_strategy_report",
    "get_brain_learnings",
    "get_performance_timeseries",
    "save_brain_learnings",
    "create_custom_report",
    "list_custom_reports",
    "save_custom_report",
    "run_saved_custom_report",
    "delete_custom_report",
    "predict_creative",
    "get_demographics_performance",
    "generate_brand_taxonomy",
    "scan_competitor",
    "generate_naming",
}
INTERNAL_BACKFILL_TOOLS = {"import_meta_performance", "import_competitor_ads"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wheel",
        type=Path,
        help="Wheel to install. Defaults to dist/creative_tagger_mcp-<version>-py3-none-any.whl.",
    )
    parser.add_argument(
        "--keep-venv",
        action="store_true",
        help="Keep the temporary virtualenv for debugging.",
    )
    args = parser.parse_args()

    version = _project_version()
    wheel = args.wheel or DIST / f"creative_tagger_mcp-{version}-py3-none-any.whl"
    sdist = DIST / f"creative_tagger_mcp-{version}.tar.gz"
    if not wheel.exists():
        raise SystemExit(f"Missing wheel: {wheel}")
    if not sdist.exists():
        raise SystemExit(f"Missing sdist: {sdist}")

    with tempfile.TemporaryDirectory(prefix="creative-tagger-mcp-smoke-") as tmp:
        venv_dir = Path(tmp) / "venv"
        _create_venv(venv_dir)
        python = _venv_python(venv_dir)
        _install_wheel(python, wheel)
        payload = _run_python_smoke(python, version)
        print(json.dumps(payload, indent=2, sort_keys=True))
        if args.keep_venv:
            keep_path = ROOT / ".release-smoke-venv"
            if keep_path.exists():
                shutil.rmtree(keep_path)
            shutil.copytree(venv_dir, keep_path)
            print(f"Kept virtualenv at {keep_path}")

    return 0


def _project_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    if tomllib is None:
        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', text)
        if not match:
            raise SystemExit(f"Could not read project version from {PYPROJECT}")
        return match.group(1)

    data = tomllib.loads(text)
    return str(data["project"]["version"])


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _create_venv(venv_dir: Path) -> None:
    if shutil.which("uv"):
        _run(["uv", "venv", str(venv_dir)])
        return
    venv.EnvBuilder(with_pip=True).create(venv_dir)


def _install_wheel(python: Path, wheel: Path) -> None:
    if shutil.which("uv"):
        _run(["uv", "pip", "install", "--python", str(python), str(wheel)])
        return
    _run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)])


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(tempfile.gettempdir()),
    )
    if proc.returncode != 0:
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        raise SystemExit(f"Command failed ({proc.returncode}): {' '.join(command)}\n{output}")
    return proc


def _run_python_smoke(python: Path, expected_version: str) -> dict:
    code = f"""
import asyncio
import importlib.metadata as metadata
import json

import creative_tagger_mcp
from creative_tagger_mcp import server

expected_tools = {sorted(EXPECTED_TOOLS)!r}
dist_version = metadata.version("creative-tagger-mcp")
entry_points = metadata.entry_points().select(
    group="console_scripts",
    name="creative-tagger-mcp",
)
tools = asyncio.run(server.list_tools())
tool_names = sorted(tool.name for tool in tools)

assert creative_tagger_mcp.__version__ == {expected_version!r}
assert dist_version == {expected_version!r}
assert len(entry_points) == 1
assert entry_points[0].value == "creative_tagger_mcp.server:main"
assert tool_names == expected_tools
internal_backfill_tools = {sorted(INTERNAL_BACKFILL_TOOLS)!r}
assert not (set(internal_backfill_tools) & set(tool_names))
assert server.API_URL == "https://api.creativetagger.ai"

print(json.dumps({{
    "version": dist_version,
    "entry_point": entry_points[0].value,
    "tool_count": len(tool_names),
    "api_url": server.API_URL,
}}))
"""
    proc = _run([str(python), "-c", code])
    return json.loads(proc.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
