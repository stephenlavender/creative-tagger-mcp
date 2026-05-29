"""Smoke tests for the documented MCP V1 tool surface.

These parse the source instead of importing it, so they can run in a clean
workspace before the optional MCP runtime dependency is installed.
"""

from __future__ import annotations

import ast
import json
from types import SimpleNamespace
import unittest
from pathlib import Path


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
    "get_meta_performance_summary",
    "get_taxonomy_performance",
    "get_demographics_performance",
    "generate_brand_taxonomy",
    "scan_competitor",
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
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name = str(kw.value.value)
            if kw.arg == "inputSchema":
                schema = ast.literal_eval(kw.value)
        if name:
            tools[name] = {"inputSchema": schema}
    return tools


def _load_pure_helpers(wanted: set[str]) -> dict:
    """Load pure helpers, without importing MCP/httpx dependencies."""
    tree = ast.parse(SERVER.read_text())
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace = {
        "json": json,
        "_text": lambda payload: [SimpleNamespace(text=json.dumps(payload, indent=2))],
    }
    exec(compile(module, str(SERVER), "exec"), namespace)
    return namespace


if __name__ == "__main__":
    unittest.main()
