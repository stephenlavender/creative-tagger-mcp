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
import tarfile
import tempfile
import venv
from pathlib import Path, PurePosixPath

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
    "list_workspaces",
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
    "export_brain_learnings_context",
    "get_performance_timeseries",
    "export_performance_timeseries_context",
    "save_brain_learnings",
    "create_custom_report",
    "list_custom_reports",
    "save_custom_report",
    "run_saved_custom_report",
    "delete_custom_report",
    "predict_creative",
    "get_demographics_performance",
    "export_demographics_context",
    "generate_brand_taxonomy",
    "scan_competitor",
    "get_competitor_scan_history",
    "generate_naming",
}
INTERNAL_BACKFILL_TOOLS = {"import_meta_performance", "import_competitor_ads"}
FORBIDDEN_SDIST_PARTS = {
    ".release-smoke-venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
}


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
    _verify_sdist_contents(sdist)

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


def _verify_sdist_contents(sdist: Path) -> None:
    """Fail closed when generated or environment files leak into the sdist."""

    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts[1:]
            forbidden = next(
                (
                    part
                    for part in parts
                    if part in FORBIDDEN_SDIST_PARTS or part.startswith(".venv")
                ),
                None,
            )
            if forbidden:
                raise SystemExit(
                    f"Forbidden build/environment path in sdist: {member.name}"
                )


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
from creative_tagger_mcp.taxonomy import taxonomy_payload

expected_tools = {sorted(EXPECTED_TOOLS)!r}
dist_version = metadata.version("creative-tagger-mcp")
package_metadata = metadata.metadata("creative-tagger-mcp")
requirements = metadata.requires("creative-tagger-mcp") or []
readme = package_metadata.get_payload()
entry_points = list(metadata.entry_points().select(
    group="console_scripts",
    name="creative-tagger-mcp",
))
tools = asyncio.run(server.list_tools())
tool_names = sorted(tool.name for tool in tools)
tool_descriptions = {{tool.name: tool.description for tool in tools}}
tools_by_name = {{tool.name: tool for tool in tools}}
tool_catalog = json.dumps(
    [tool.model_dump(exclude_none=True) for tool in tools],
    separators=(",", ":"),
)
taxonomy = taxonomy_payload()
initialization = server.server.create_initialization_options()

assert creative_tagger_mcp.__version__ == {expected_version!r}
assert dist_version == {expected_version!r}
assert any(requirement.startswith("mcp<2,>=1.28.1") for requirement in requirements)
assert initialization.server_version == {expected_version!r}
assert "call list_workspaces first" in initialization.instructions
assert "historical associations" in initialization.instructions
assert "packaged metadata are" in readme
assert "version `0.2.2`" in readme
assert "pip install creative-tagger-mcp==0.2.2" in readme
assert "unreleased `0.2.2` candidate" not in readme
assert "pip install creative-tagger-mcp==0.2.1" not in readme
assert "PyPI still serves `creative-tagger-mcp==0.1.0`" not in readme
assert "`higher_observed_efficiency`" in readme
assert "`lower_observed_efficiency`" in readme
assert len(entry_points) == 1
assert entry_points[0].value == "creative_tagger_mcp.server:main"
assert tool_names == expected_tools
assert len(tool_catalog) < 40_000
assert "opportunity" not in tool_catalog.lower()
assert "waste" not in tool_catalog.lower()
strategy_schema = tools_by_name["get_creative_strategy_report"].inputSchema["properties"]
assert strategy_schema["response_format"]["default"] == "concise"
assert strategy_schema["max_cells"]["default"] == 24
library_schema = tools_by_name["list_library"].inputSchema["properties"]
assert library_schema["limit"]["minimum"] == 1
assert library_schema["limit"]["maximum"] == 100
assert library_schema["offset"]["minimum"] == 0
brain_schema = tools_by_name["get_brain_learnings"].inputSchema["properties"]
assert brain_schema["audience_signal_focus"]["enum"] == [
    "all", "higher_observed_efficiency", "lower_observed_efficiency"
]
timeseries_schema = tools_by_name["get_performance_timeseries"].inputSchema["properties"]
assert timeseries_schema["limit"]["minimum"] == 1
assert timeseries_schema["limit"]["maximum"] == 100
demographics_export_schema = tools_by_name["export_demographics_context"].inputSchema[
    "properties"
]
assert demographics_export_schema["limit"]["minimum"] == 1
assert demographics_export_schema["limit"]["maximum"] == 100
assert "brand_name" in tools_by_name["get_meta_status"].inputSchema["properties"]
internal_backfill_tools = {sorted(INTERNAL_BACKFILL_TOOLS)!r}
assert not (set(internal_backfill_tools) & set(tool_names))
assert server.API_URL == "https://api.creativetagger.ai"
assert taxonomy["controlled_dimension_count"] == 15
assert taxonomy["derived_open_dimension_count"] == 1
assert taxonomy["dynamic_dimension_count"] == 2
assert "aspect_ratio" not in taxonomy["controlled_dimensions"]
aspect_ratio = taxonomy["derived_open_dimensions"]["aspect_ratio"]
assert aspect_ratio["allow_other_values"] is True
assert "300x157" in aspect_ratio["canonical_values"]
assert "derived/open aspect-ratio dimension" in tool_descriptions["get_taxonomy"]
assert "brand presence" not in tool_descriptions["analyze_creative"]
assert "social proof" not in tool_descriptions["analyze_creative"].lower()

print(json.dumps({{
    "version": dist_version,
    "server_version": initialization.server_version,
    "entry_point": entry_points[0].value,
    "tool_count": len(tool_names),
    "tool_catalog_bytes": len(tool_catalog),
    "controlled_dimension_count": taxonomy["controlled_dimension_count"],
    "derived_open_dimension_count": taxonomy["derived_open_dimension_count"],
    "dynamic_dimension_count": taxonomy["dynamic_dimension_count"],
    "api_url": server.API_URL,
}}))
"""
    proc = _run([str(python), "-c", code])
    return json.loads(proc.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
