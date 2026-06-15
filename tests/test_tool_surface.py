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

PUBLIC_EXPECTED_TOOLS = {
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
    "save_brain_learnings",
    "get_performance_timeseries",
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
EXPECTED_DECLARED_TOOLS = PUBLIC_EXPECTED_TOOLS | INTERNAL_BACKFILL_TOOLS


class ToolSurfaceTest(unittest.TestCase):
    def test_v1_tools_are_declared(self) -> None:
        names = _declared_tool_names()

        self.assertEqual(names, EXPECTED_DECLARED_TOOLS)

    def test_public_tool_surface_excludes_internal_backfills_by_default(self) -> None:
        source = SERVER.read_text()

        self.assertIn("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", source)
        self.assertIn("INTERNAL_BACKFILL_TOOLS", source)
        self.assertIn("_visible_tools", source)
        self.assertIn("not _is_internal_backfill_enabled()", source)

    def test_every_declared_tool_is_dispatched(self) -> None:
        source = SERVER.read_text()

        for name in EXPECTED_DECLARED_TOOLS:
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

    def test_performance_tools_describe_funnel_scores(self) -> None:
        tools = _declared_tools()

        summary_desc = tools["get_meta_performance_summary"]["description"]
        taxonomy_desc = tools["get_taxonomy_performance"]["description"]
        prebuilt_desc = tools["get_prebuilt_reports"]["description"]
        strategy_desc = tools["get_creative_strategy_report"]["description"]
        brain_desc = tools["get_brain_learnings"]["description"]
        brain_save_desc = tools["save_brain_learnings"]["description"]
        timeseries_desc = tools["get_performance_timeseries"]["description"]
        demographics_desc = tools["get_demographics_performance"]["description"]
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
        self.assertIn("strategist matrix", strategy_desc)
        self.assertIn("agent_context", strategy_desc)
        self.assertIn("hook", strategy_desc)
        self.assertIn("hold", strategy_desc)
        self.assertIn("demographic_age", strategy_desc)
        strategy_schema = tools["get_creative_strategy_report"]["inputSchema"]["properties"]
        self.assertIn("messaging_angle", strategy_schema["rows"]["description"])
        self.assertIn("demographic_gender", strategy_schema["columns"]["description"])
        self.assertIn("demographic-read", strategy_schema["report_template"]["description"])
        self.assertEqual(
            strategy_schema["metrics"]["default"],
            "spend,ctr,thumbstop_rate,hook_rate,hold_rate,cpa",
        )
        self.assertIn("hook_rate", strategy_schema["metrics"]["description"])
        self.assertIn("YYYY-MM-DD", strategy_schema["start_date"]["description"])
        self.assertIn("roas_target", strategy_schema)
        self.assertIn("Brand Brain learnings", brain_desc)
        self.assertIn("agent_context", brain_desc)
        self.assertIn("audience opportunities", brain_desc)
        self.assertIn("conclusion-only", brain_desc)
        self.assertIn("working-only", brain_desc)
        self.assertIn("Persist", brain_save_desc)
        self.assertIn("Brand Brain notes", brain_save_desc)
        brain_save_schema = tools["save_brain_learnings"]["inputSchema"]["properties"]
        self.assertEqual(brain_save_schema["limit"]["default"], 8)
        self.assertEqual(brain_save_schema["include_gaps_in_notes"]["default"], False)
        self.assertEqual(brain_save_schema["date_preset"]["default"], "all_time")
        self.assertIn("YYYY-MM-DD", brain_save_schema["start_date"]["description"])
        self.assertEqual(brain_save_schema["watch_group_by"]["default"], "messaging_angle")
        self.assertEqual(brain_save_schema["watch_metric"]["default"], "roas")
        self.assertEqual(brain_save_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_save_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertIn("strategy", brain_save_schema["watch_sources"]["description"])
        brain_schema = tools["get_brain_learnings"]["inputSchema"]["properties"]
        self.assertEqual(brain_schema["limit"]["default"], 8)
        self.assertEqual(brain_schema["date_preset"]["default"], "all_time")
        self.assertIn("YYYY-MM-DD", brain_schema["start_date"]["description"])
        self.assertIn("conclusion, working, watch, audience, gap", brain_schema["kinds"]["description"])
        self.assertIn("conclusion, working, watch, audience, gap", brain_save_schema["kinds"]["description"])
        self.assertEqual(brain_schema["watch_group_by"]["default"], "messaging_angle")
        self.assertEqual(brain_schema["watch_metric"]["default"], "roas")
        self.assertEqual(brain_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertIn("visual_style", brain_schema["watch_group_by"]["description"])
        self.assertIn("thumbstop_rate", brain_schema["watch_metric"]["description"])
        self.assertIn("timeseries", brain_schema["watch_sources"]["description"])
        self.assertIn("patterns", brain_schema["watch_sources"]["description"])
        self.assertIn("fatigue", timeseries_desc)
        self.assertIn("thumbstop", timeseries_desc)
        self.assertIn("analysis id", timeseries_desc)
        self.assertIn("visual style", timeseries_desc)
        timeseries_schema = tools["get_performance_timeseries"]["inputSchema"]["properties"]
        self.assertEqual(timeseries_schema["group_by"]["default"], "ad_name")
        self.assertEqual(timeseries_schema["date_preset"]["default"], "last_30d")
        self.assertEqual(timeseries_schema["metric"]["default"], "roas")
        self.assertEqual(timeseries_schema["signal_focus"]["default"], "all")
        self.assertEqual(timeseries_schema["minimum_spend"]["default"], 500)
        self.assertEqual(timeseries_schema["minimum_points"]["default"], 0)
        self.assertEqual(timeseries_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertIn("last_90d", timeseries_schema["date_preset"]["description"])
        self.assertIn("landing_page_domain", timeseries_schema["group_by"]["description"])
        self.assertIn("visual_style", timeseries_schema["group_by"]["description"])
        self.assertIn("cta", timeseries_schema["group_by"]["description"])
        self.assertIn("funnel_score", timeseries_schema["metric"]["description"])
        self.assertIn("fatigued", timeseries_schema["signal_focus"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_desc)
        demographics_schema = tools["get_demographics_performance"]["inputSchema"]["properties"]
        self.assertIn("start_date", demographics_schema)
        self.assertIn("end_date", demographics_schema)
        self.assertIn("YYYY-MM-DD", demographics_schema["start_date"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_schema["end_date"]["description"])
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

    def test_strategy_tool_forwards_demographic_and_date_controls(self) -> None:
        source = SERVER.read_text()

        self.assertIn('"start_date": args.get("start_date", "")', source)
        self.assertIn('"end_date": args.get("end_date", "")', source)
        self.assertIn('"rows": args.get("rows", "messaging_angle")', source)
        self.assertIn('"columns": args.get("columns", "ad_type")', source)
        self.assertIn('"roas_target"', source)
        self.assertIn('"watch_group_by"', source)
        self.assertIn('"watch_metric"', source)
        self.assertIn('"watch_minimum_points"', source)
        self.assertIn('"watch_sources"', source)
        self.assertIn('"minimum_points": args.get("minimum_points", 0)', source)
        self.assertIn('"fatigue_decay_threshold"', source)
        self.assertIn('"date_preset": args.get("date_preset", "all_time")', source)
        self.assertIn('"date_preset": args.get("date_preset", "last_30d")', source)
        self.assertIn('f"{API_URL}/performance/demographics"', source)
        self.assertIn('if name == "save_brain_learnings":', source)
        self.assertIn('f"{API_URL}/brain/learnings/save"', source)

    def test_competitor_import_tool_is_positioned_as_gated_backfill(self) -> None:
        tools = _declared_tools()
        import_tool = tools["import_competitor_ads"]
        props = import_tool["inputSchema"]["properties"]

        self.assertIn("CREATIVE_TAGGER_INTERNAL_BACKFILL_TOOLS", SERVER.read_text())
        self.assertIn("internal backfills", import_tool["description"])
        self.assertIn("scan_competitor", import_tool["description"])
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
        "json": json,
        "_text": lambda payload: [SimpleNamespace(text=json.dumps(payload, indent=2))],
    }
    exec(compile(module, str(SERVER), "exec"), namespace)
    return namespace


if __name__ == "__main__":
    unittest.main()
