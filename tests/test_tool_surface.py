"""Smoke tests for the documented MCP V1 tool surface.

These parse the source instead of importing it, so they can run in a clean
workspace before the optional MCP runtime dependency is installed.
"""

from __future__ import annotations

import ast
import csv
import json
import os
import unittest
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "creative_tagger_mcp" / "server.py"

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
    "import_meta_performance",
    "get_meta_performance_summary",
    "get_taxonomy_performance",
    "get_prebuilt_reports",
    "create_custom_report",
    "list_custom_reports",
    "save_custom_report",
    "run_saved_custom_report",
    "delete_custom_report",
    "predict_creative",
    "get_demographics_performance",
    "generate_brand_taxonomy",
    "scan_competitor",
    "import_competitor_ads",
    "generate_naming",
}


class ToolSurfaceTest(unittest.TestCase):
    def test_v1_tools_are_declared(self) -> None:
        names = _declared_tool_names()

        self.assertEqual(names, EXPECTED_TOOLS)

    def test_every_declared_tool_is_dispatched(self) -> None:
        source = SERVER.read_text()

        for name in EXPECTED_TOOLS:
            self.assertIn(f'if name == "{name}":', source)

    def test_package_version_matches_v2_surface(self) -> None:
        init_file = ROOT / "src" / "creative_tagger_mcp" / "__init__.py"
        self.assertIn('__version__ = "0.2.0"', init_file.read_text())

    def test_publish_workflow_verifies_release_before_upload(self) -> None:
        workflow = ROOT / ".github" / "workflows" / "publish.yml"
        source = workflow.read_text()

        self.assertIn("python -m build", source)
        self.assertIn("python scripts/smoke_release.py", source)
        self.assertIn("python -m twine check dist/*", source)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", source)
        self.assertIn("id-token: write", source)
        self.assertIn("PYPI_API_TOKEN", source)

    def test_release_smoke_does_not_require_tomli_on_old_python(self) -> None:
        smoke = ROOT / "scripts" / "smoke_release.py"
        source = smoke.read_text()

        self.assertIn("tomllib = None", source)
        self.assertIn("_project_version", source)
        self.assertNotIn("import tomli", source)

    def test_generate_naming_matches_v1_api_shape(self) -> None:
        namespace = _load_pure_helpers({"_generate_naming", "_sanitize", "_ratio", "_join"})

        result = namespace["_generate_naming"](
            {
                "brand_name": "Creative Tagger",
                "asset_type": "UGC",
                "visual_format": "Talking Head",
                "visual_style": "Organic",
                "talent_type": "Founder",
                "audience": "Creative Strategists",
                "messaging_angle": "Pain Point",
                "hook_type": "Question",
                "cta_type": "Shop Now",
                "offer_type": "No Offer",
                "seasonality": "Evergreen",
                "audio_type": "Voiceover + Music",
                "aspect_ratio": "9:16",
                "duration": "30s",
                "version": 2,
            }
        )

        payload = json.loads(result[0].text)
        self.assertEqual(
            payload["standard"],
            "CREATIVE TAGGER_UGC_TalkingHead_Founder_Question_ShopNow_9x16_V2",
        )
        self.assertEqual(
            payload["compact"],
            "CREATIVE TAGGER_TalkingHead_Founder_ShopNow_9x16_V2",
        )
        self.assertEqual(
            payload["reporting"],
            "CREATIVE TAGGER_UGC_TalkingHead_CreativeStrategists_"
            "PainPoint_Question_Evergreen_V2",
        )
        self.assertEqual(payload["variables"]["aspect_ratio"], "9x16")

    def test_analyze_creative_declares_carousel_and_version_inputs(self) -> None:
        tools = _declared_tools()
        analyze = tools["analyze_creative"]
        props = analyze["inputSchema"]["properties"]
        one_of = analyze["inputSchema"]["oneOf"]

        self.assertIn("file_paths", props)
        self.assertIn("version", props)
        self.assertIn("format", props)
        self.assertIn("include_transcript", props)
        self.assertIn("forensic_mode", props)
        self.assertIn({"required": ["file_paths"]}, one_of)

    def test_analysis_form_data_matches_api_fields(self) -> None:
        namespace = _load_pure_helpers({"_analysis_form_data"})

        data = namespace["_analysis_form_data"](
            {
                "version": 3,
                "format": "carousel",
                "include_transcript": False,
                "forensic_mode": True,
            },
            "Creative Tagger",
        )

        self.assertEqual(
            data,
            {
                "brand_name": "Creative Tagger",
                "version": "3",
                "format": "carousel",
                "include_transcript": "false",
                "forensic_mode": "true",
            },
        )

    def test_import_meta_performance_accepts_file_path(self) -> None:
        tools = _declared_tools()
        import_tool = tools["import_meta_performance"]
        props = import_tool["inputSchema"]["properties"]

        self.assertIn("file_path", props)
        self.assertIn({"required": ["file_path"]}, import_tool["inputSchema"]["oneOf"])
        self.assertIn("CSV, JSON, or JSONL", props["file_path"]["description"])

    def test_load_rows_file_supports_csv_json_and_jsonl(self) -> None:
        namespace = _load_pure_helpers({"_load_rows_file"})

        with NamedTemporaryFile("w", suffix=".csv", encoding="utf-8", delete=False) as handle:
            writer = csv.DictWriter(handle, fieldnames=["ad_name", "spend"])
            writer.writeheader()
            writer.writerow({"ad_name": "Creative A", "spend": "10"})
            csv_path = handle.name

        with NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump({"rows": [{"ad_name": "Creative B", "spend": 11}]}, handle)
            json_path = handle.name

        with NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as handle:
            handle.write('{"ad_name":"Creative C","spend":12}\n')
            jsonl_path = handle.name

        try:
            self.assertEqual(namespace["_load_rows_file"](csv_path), [{"ad_name": "Creative A", "spend": "10"}])
            self.assertEqual(namespace["_load_rows_file"](json_path), [{"ad_name": "Creative B", "spend": 11}])
            self.assertEqual(namespace["_load_rows_file"](jsonl_path), [{"ad_name": "Creative C", "spend": 12}])
        finally:
            os.unlink(csv_path)
            os.unlink(json_path)
            os.unlink(jsonl_path)

    def test_coerce_rows_input_rejects_ambiguous_or_invalid_inputs(self) -> None:
        namespace = _load_pure_helpers({"_coerce_rows_input", "_load_rows_file"})

        with self.assertRaisesRegex(ValueError, "either rows or file_path"):
            namespace["_coerce_rows_input"](
                {"rows": [{"ad_name": "Creative A"}], "file_path": "/tmp/export.json"},
                key="rows",
                file_key="file_path",
            )

        with self.assertRaisesRegex(ValueError, "rows or file_path is required"):
            namespace["_coerce_rows_input"]({}, key="rows", file_key="file_path")

    def test_performance_tools_describe_funnel_scores(self) -> None:
        tools = _declared_tools()

        summary_desc = tools["get_meta_performance_summary"]["description"]
        taxonomy_desc = tools["get_taxonomy_performance"]["description"]
        prebuilt_desc = tools["get_prebuilt_reports"]["description"]
        custom_desc = tools["create_custom_report"]["description"]
        saved_desc = tools["save_custom_report"]["description"]
        import_rows = (
            tools["import_meta_performance"]["inputSchema"]["properties"]["rows"]
        )

        self.assertIn("funnel_score", summary_desc)
        self.assertIn("capture", summary_desc)
        self.assertIn("funnel_score", taxonomy_desc)
        self.assertIn("thumbstop", taxonomy_desc)
        self.assertIn("best hooks", prebuilt_desc)
        self.assertIn("landing pages", prebuilt_desc)
        self.assertIn("custom performance report", custom_desc)
        self.assertIn("dimension combinations", custom_desc)
        self.assertIn("hook x landing_page x offer_type", custom_desc)
        self.assertIn(
            "landing_page",
            tools["create_custom_report"]["inputSchema"]["properties"]["dimensions"][
                "description"
            ],
        )
        self.assertIn("reusable custom report", saved_desc)
        self.assertIn("hook_type x landing_page x offer_type", saved_desc)
        self.assertIn("video_p100", import_rows["description"])

    def test_competitor_import_tool_documents_approval_workaround(self) -> None:
        tools = _declared_tools()
        import_tool = tools["import_competitor_ads"]
        props = import_tool["inputSchema"]["properties"]

        self.assertIn("native Meta Ad Library token/app approval", import_tool["description"])
        self.assertIn("ads", import_tool["inputSchema"]["required"])
        self.assertIn("spend_lower", props["ads"]["description"])
        self.assertIn("ad_id", props["analyses"]["description"])


def _declared_tool_names() -> set[str]:
    return set(_declared_tools())


def _declared_tools() -> dict[str, dict]:
    tree = ast.parse(SERVER.read_text())
    tools: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "id", "")
        if func_name != "Tool":
            continue
        name = ""
        schema: dict = {}
        description = ""
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name = str(kw.value.value)
            if kw.arg == "description":
                description = _literal_string(kw.value)
            if kw.arg == "inputSchema":
                schema = ast.literal_eval(kw.value)
        if name:
            tools[name] = {"description": description, "inputSchema": schema}
    return tools


def _literal_string(node: ast.AST) -> str:
    value = ast.literal_eval(node)
    if isinstance(value, str):
        return value
    return str(value)


def _load_pure_helpers(wanted: set[str]) -> dict:
    """Load pure helpers, without importing MCP/httpx dependencies."""
    tree = ast.parse(SERVER.read_text())
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace = {
        "csv": csv,
        "json": json,
        "Path": Path,
        "_text": lambda payload: [SimpleNamespace(text=json.dumps(payload, indent=2))],
    }
    exec(compile(module, str(SERVER), "exec"), namespace)
    return namespace


if __name__ == "__main__":
    unittest.main()
