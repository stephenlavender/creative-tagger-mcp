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
    "export_brain_learnings_context",
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
    "get_competitor_scan_history",
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

    def test_csv_arg_normalizes_multiselect_values(self) -> None:
        namespace = _load_pure_helpers({"_csv_arg"})

        self.assertEqual(namespace["_csv_arg"](None), "")
        self.assertEqual(namespace["_csv_arg"](" winner,fatigued "), "winner,fatigued")
        self.assertEqual(
            namespace["_csv_arg"](["winner", " fatigued ", "", None, "loser"]),
            "winner,fatigued,loser",
        )
        self.assertEqual(
            namespace["_csv_arg"](("timeseries", "patterns")),
            "timeseries,patterns",
        )

    def test_strategy_params_preserve_template_defaults_unless_overridden(self) -> None:
        namespace = _load_pure_helpers({"_csv_arg", "_strategy_params"})
        strategy_params = namespace["_strategy_params"]

        template_only = strategy_params({"brand_name": "Acme", "report_template": "audience-signals"})
        self.assertEqual(template_only["report_template"], "audience-signals")
        self.assertNotIn("rows", template_only)
        self.assertNotIn("columns", template_only)
        self.assertNotIn("status_focus", template_only)
        self.assertNotIn("metrics", template_only)
        self.assertNotIn("metric_preset", template_only)

        explicit = strategy_params(
            {
                "brand_name": "Acme",
                "report_template": "audience-signals",
                "rows": "messaging_angle",
                "columns": "demographic_segment",
                "status_focus": "winner",
                "metrics": ["spend", "roas"],
                "metric_preset": "scale",
            }
        )
        self.assertEqual(explicit["rows"], "messaging_angle")
        self.assertEqual(explicit["columns"], "demographic_segment")
        self.assertEqual(explicit["status_focus"], "winner")
        self.assertEqual(explicit["metrics"], "spend,roas")
        self.assertEqual(explicit["metric_preset"], "scale")

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

    def test_list_library_supports_performance_sorts_and_facets(self) -> None:
        tools = _declared_tools()
        library = tools["list_library"]
        desc = library["description"]
        props = library["inputSchema"]["properties"]
        source = SERVER.read_text()

        self.assertIn("joined performance", desc)
        self.assertIn("ROAS", desc)
        self.assertIn("reach", desc)
        self.assertIn("frequency", desc)
        self.assertIn("CPM", desc)
        self.assertIn("angle", desc)
        self.assertIn("emotion", desc)
        self.assertIn("CTA", desc)
        self.assertIn("talent", desc)
        self.assertIn("offer", desc)
        self.assertIn("audio", desc)
        self.assertIn("season", desc)
        self.assertEqual(props["sort"]["default"], "recent")
        self.assertIn("spend", props["sort"]["description"])
        self.assertIn("reach", props["sort"]["description"])
        self.assertIn("roas", props["sort"]["description"])
        self.assertIn("ctr", props["sort"]["description"])
        self.assertIn("frequency", props["sort"]["description"])
        self.assertIn("cpm", props["sort"]["description"])
        self.assertIn("cpa", props["sort"]["description"])
        self.assertEqual(
            props["sort"]["enum"],
            ["recent", "spend", "reach", "roas", "ctr", "frequency", "cpm", "cpa"],
        )
        self.assertIn("messaging angle", props["angle"]["description"])
        self.assertIn("emotion", props["emotion"]["description"])
        self.assertIn("CTA", props["cta"]["description"])
        self.assertIn("talent", props["talent"]["description"])
        self.assertIn("offer", props["offer"]["description"])
        self.assertIn("audio", props["audio"]["description"])
        self.assertIn("season", props["season"]["description"])
        self.assertIn('"angle"', source)
        self.assertIn('"emotion"', source)
        self.assertIn('"cta"', source)
        self.assertIn('"talent"', source)
        self.assertIn('"offer"', source)
        self.assertIn('"audio"', source)
        self.assertIn('"season"', source)
        self.assertIn('"sort"', source)

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

    def test_coerce_bool_handles_stringified_false_values(self) -> None:
        namespace = _load_pure_helpers({"_coerce_bool"})
        coerce = namespace["_coerce_bool"]

        self.assertIs(coerce(False), False)
        self.assertIs(coerce(True), True)
        self.assertIs(coerce("false"), False)
        self.assertIs(coerce("0"), False)
        self.assertIs(coerce("off"), False)
        self.assertIs(coerce("true"), True)
        self.assertIs(coerce("1"), True)
        self.assertIs(coerce(None, default=True), True)

    def test_performance_tools_describe_funnel_scores(self) -> None:
        tools = _declared_tools()

        summary_desc = tools["get_meta_performance_summary"]["description"]
        taxonomy_desc = tools["get_taxonomy_performance"]["description"]
        prebuilt_desc = tools["get_prebuilt_reports"]["description"]
        strategy_desc = tools["get_creative_strategy_report"]["description"]
        brain_desc = tools["get_brain_learnings"]["description"]
        brain_save_desc = tools["save_brain_learnings"]["description"]
        brain_export_desc = tools["export_brain_learnings_context"]["description"]
        timeseries_desc = tools["get_performance_timeseries"]["description"]
        demographics_desc = tools["get_demographics_performance"]["description"]
        competitor_desc = tools["scan_competitor"]["description"]
        competitor_history_desc = tools["get_competitor_scan_history"]["description"]
        custom_desc = tools["create_custom_report"]["description"]
        saved_desc = tools["save_custom_report"]["description"]
        import_rows = (
            tools["import_meta_performance"]["inputSchema"]["properties"]["rows"]
        )

        self.assertIn("funnel_score", summary_desc)
        self.assertIn("capture", summary_desc)
        self.assertIn("last_30_days", summary_desc)
        self.assertIn("funnel_score", taxonomy_desc)
        self.assertIn("thumbstop", taxonomy_desc)
        self.assertIn("date presets", taxonomy_desc)
        self.assertIn("best hooks", prebuilt_desc)
        self.assertIn("landing pages", prebuilt_desc)
        self.assertIn("YYYY-MM-DD", prebuilt_desc)
        prebuilt_schema = tools["get_prebuilt_reports"]["inputSchema"]["properties"]
        self.assertEqual(prebuilt_schema["limit"]["default"], 8)
        self.assertIn("start_date", prebuilt_schema)
        self.assertIn("end_date", prebuilt_schema)
        self.assertIn("YYYY-MM-DD", prebuilt_schema["start_date"]["description"])
        self.assertIn("YYYY-MM-DD", prebuilt_schema["end_date"]["description"])
        self.assertIn("strategist matrix", strategy_desc)
        self.assertIn("agent_context", strategy_desc)
        self.assertIn("hook", strategy_desc)
        self.assertIn("hold", strategy_desc)
        self.assertIn("demographic_age", strategy_desc)
        self.assertIn("mixed creative x audience", strategy_desc)
        self.assertIn("messaging_angle by demographic_segment", strategy_desc)
        strategy_schema = tools["get_creative_strategy_report"]["inputSchema"]["properties"]
        self.assertEqual(strategy_schema["date_preset"]["default"], "all_time")
        self.assertIn("last_30_days", strategy_schema["date_preset"]["description"])
        self.assertEqual(strategy_schema["rows"]["default"], "ad_type")
        self.assertEqual(strategy_schema["columns"]["default"], "messaging_angle")
        self.assertIn("ad_type", strategy_schema["rows"]["description"])
        self.assertIn("offer_type", strategy_schema["rows"]["description"])
        self.assertIn("messaging_angle", strategy_schema["columns"]["description"])
        self.assertIn("persona", strategy_schema["columns"]["description"])
        self.assertIn("mixed audience reads", strategy_schema["rows"]["description"])
        self.assertIn("mixed creative x audience matrix", strategy_schema["columns"]["description"])
        self.assertIn("demographic_gender", strategy_schema["columns"]["description"])
        self.assertIn("demographic_segment", strategy_schema["columns"]["description"])
        self.assertNotIn("funnel_stage", strategy_schema["columns"]["description"])
        self.assertIn("demographic-read", strategy_schema["report_template"]["description"])
        self.assertIn("audience-signals", strategy_schema["report_template"]["description"])
        self.assertIn("hook-performance", strategy_schema["report_template"]["description"])
        self.assertIn("coverage-gaps", strategy_schema["report_template"]["description"])
        self.assertIn("messaging_angle by demographic_segment", strategy_schema["report_template"]["description"])
        self.assertIn("metric_preset", strategy_schema)
        self.assertIn("delivery", strategy_schema["metric_preset"]["description"])
        self.assertEqual(strategy_schema["fatigue_minimum_calendar_days"]["default"], 0)
        self.assertIn(
            "fatigue read is treated as meaningful",
            strategy_schema["fatigue_minimum_calendar_days"]["description"],
        )
        self.assertEqual(strategy_schema["watch_signal_focus"]["default"], "all")
        self.assertEqual(strategy_schema["watch_trajectory_focus"]["default"], "all")
        self.assertEqual(strategy_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(strategy_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(strategy_schema["watch_limit"]["default"], 5)
        self.assertIn("messaging_angle", strategy_schema["watch_group_by"]["description"])
        self.assertIn("demographic_segment", strategy_schema["watch_group_by"]["description"])
        self.assertIn("thumbstop_rate", strategy_schema["watch_metric"]["description"])
        self.assertIn("fatigued", strategy_schema["watch_signal_focus"]["description"])
        self.assertIn("worsening", strategy_schema["watch_trajectory_focus"]["description"])
        self.assertIn("fatigue cadence gate", strategy_schema["watch_minimum_calendar_days"]["description"])
        self.assertIn("sync gap", strategy_schema["watch_maximum_gap_days"]["description"])
        self.assertIn("fatigue watch groups", strategy_schema["watch_limit"]["description"])
        self.assertEqual(
            strategy_schema["metrics"]["default"],
            "spend,ctr,thumbstop_rate,hook_rate,hold_rate,cpa",
        )
        self.assertIn("hook_rate", strategy_schema["metrics"]["description"])
        self.assertIn("YYYY-MM-DD", strategy_schema["start_date"]["description"])
        self.assertIn("roas_target", strategy_schema)
        self.assertIn("Meta Ad Library", competitor_desc)
        competitor_schema = tools["scan_competitor"]["inputSchema"]["properties"]
        self.assertIn("brand_name", competitor_schema)
        self.assertIn("saved competitor Market scans", competitor_history_desc)
        competitor_history_schema = tools["get_competitor_scan_history"]["inputSchema"]["properties"]
        self.assertEqual(competitor_history_schema["limit"]["default"], 10)
        self.assertIn("brand_name", competitor_history_schema)
        self.assertIn("Brand Brain learnings", brain_desc)
        self.assertIn("agent_context", brain_desc)
        self.assertIn("audience opportunities", brain_desc)
        self.assertIn("conclusion-only", brain_desc)
        self.assertIn("working-only", brain_desc)
        self.assertIn("demographic_segment", brain_desc)
        self.assertIn("waste-only", brain_desc)
        self.assertIn("Persist", brain_save_desc)
        self.assertIn("Brand Brain notes", brain_save_desc)
        self.assertIn("demographic_signal", brain_save_desc)
        self.assertIn("opportunities-only", brain_save_desc)
        self.assertIn("agent_context payload", brain_export_desc)
        self.assertIn("brief-ready prompt seed", brain_export_desc)
        self.assertIn("saved Brand Brain context", brain_export_desc)
        brain_export_schema = tools["export_brain_learnings_context"]["inputSchema"]["properties"]
        self.assertEqual(brain_export_schema["limit"]["default"], 8)
        self.assertEqual(brain_export_schema["date_preset"]["default"], "all_time")
        self.assertIn("YYYY-MM-DD", brain_export_schema["start_date"]["description"])
        self.assertEqual(brain_export_schema["watch_group_by"]["default"], "messaging_angle")
        self.assertEqual(brain_export_schema["watch_metric"]["default"], "roas")
        self.assertEqual(brain_export_schema["watch_signal_focus"]["default"], "all")
        self.assertEqual(brain_export_schema["watch_trajectory_focus"]["default"], "all")
        self.assertEqual(brain_export_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_export_schema["watch_minimum_calendar_days"]["default"], 0)
        self.assertEqual(brain_export_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(brain_export_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(brain_export_schema["audience_signal_focus"]["default"], "all")
        self.assertEqual(brain_export_schema["audience_limit"]["default"], 3)
        self.assertIn("conclusion, working, watch, audience, gap", brain_export_schema["kinds"]["description"])
        self.assertIn("demographic_segment", brain_export_schema["watch_group_by"]["description"])
        self.assertIn("thumbstop_rate", brain_export_schema["watch_metric"]["description"])
        self.assertIn("worsening", brain_export_schema["watch_trajectory_focus"]["description"])
        self.assertIn("strategy", brain_export_schema["watch_sources"]["description"])
        self.assertIn("waste", brain_export_schema["audience_signal_focus"]["description"])
        brain_save_schema = tools["save_brain_learnings"]["inputSchema"]["properties"]
        self.assertEqual(brain_save_schema["limit"]["default"], 8)
        self.assertEqual(brain_save_schema["include_gaps_in_notes"]["default"], False)
        self.assertEqual(brain_save_schema["date_preset"]["default"], "all_time")
        self.assertIn("YYYY-MM-DD", brain_save_schema["start_date"]["description"])
        self.assertEqual(brain_save_schema["watch_group_by"]["default"], "messaging_angle")
        self.assertEqual(brain_save_schema["watch_metric"]["default"], "roas")
        self.assertEqual(brain_save_schema["watch_signal_focus"]["default"], "all")
        self.assertEqual(brain_save_schema["watch_trajectory_focus"]["default"], "all")
        self.assertEqual(brain_save_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_save_schema["watch_minimum_calendar_days"]["default"], 0)
        self.assertEqual(brain_save_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(brain_save_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(brain_save_schema["audience_signal_focus"]["default"], "all")
        self.assertEqual(brain_save_schema["audience_limit"]["default"], 3)
        self.assertIn("winner", brain_save_schema["conclusion_statuses"]["description"])
        self.assertIn("fatigued", brain_save_schema["conclusion_statuses"]["description"])
        self.assertIn("opportunity", brain_save_schema["audience_signal_focus"]["description"])
        self.assertIn("waste", brain_save_schema["audience_signal_focus"]["description"])
        self.assertIn("strategy", brain_save_schema["watch_sources"]["description"])
        self.assertIn("fatigued", brain_save_schema["watch_signal_focus"]["description"])
        self.assertIn("worsening", brain_save_schema["watch_trajectory_focus"]["description"])
        brain_schema = tools["get_brain_learnings"]["inputSchema"]["properties"]
        self.assertEqual(brain_schema["limit"]["default"], 8)
        self.assertEqual(brain_schema["date_preset"]["default"], "all_time")
        self.assertIn("YYYY-MM-DD", brain_schema["start_date"]["description"])
        self.assertIn("conclusion, working, watch, audience, gap", brain_schema["kinds"]["description"])
        self.assertIn("conclusion, working, watch, audience, gap", brain_save_schema["kinds"]["description"])
        self.assertEqual(brain_schema["watch_group_by"]["default"], "messaging_angle")
        self.assertEqual(brain_schema["watch_metric"]["default"], "roas")
        self.assertEqual(brain_schema["watch_signal_focus"]["default"], "all")
        self.assertEqual(brain_schema["watch_trajectory_focus"]["default"], "all")
        self.assertEqual(brain_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_schema["watch_minimum_calendar_days"]["default"], 0)
        self.assertEqual(brain_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(brain_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(brain_schema["audience_signal_focus"]["default"], "all")
        self.assertEqual(brain_schema["audience_limit"]["default"], 3)
        self.assertIn("loser", brain_schema["conclusion_statuses"]["description"])
        self.assertIn("all", brain_schema["conclusion_statuses"]["description"])
        self.assertIn("opportunity", brain_schema["audience_signal_focus"]["description"])
        self.assertIn("waste", brain_schema["audience_signal_focus"]["description"])
        self.assertIn("visual_style", brain_schema["watch_group_by"]["description"])
        self.assertIn("demographic_age", brain_schema["watch_group_by"]["description"])
        self.assertIn("demographic_signal", brain_save_schema["watch_group_by"]["description"])
        self.assertIn("thumbstop_rate", brain_schema["watch_metric"]["description"])
        self.assertIn("stable", brain_schema["watch_signal_focus"]["description"])
        self.assertIn("improving", brain_schema["watch_trajectory_focus"]["description"])
        self.assertIn("sync gap", brain_schema["watch_maximum_gap_days"]["description"])
        self.assertIn("timeseries", brain_schema["watch_sources"]["description"])
        self.assertIn("patterns", brain_schema["watch_sources"]["description"])
        self.assertIn("fatigue", timeseries_desc)
        self.assertIn("thumbstop", timeseries_desc)
        self.assertIn("analysis id", timeseries_desc)
        self.assertIn("audience slice", timeseries_desc)
        self.assertIn("visual style", timeseries_desc)
        self.assertIn("worsening", timeseries_desc)
        summary_schema = tools["get_meta_performance_summary"]["inputSchema"]["properties"]
        self.assertEqual(summary_schema["date_preset"]["default"], "all_time")
        self.assertIn("last_30_days", summary_schema["date_preset"]["description"])
        self.assertIn("YYYY-MM-DD", summary_schema["start_date"]["description"])
        taxonomy_schema = tools["get_taxonomy_performance"]["inputSchema"]["properties"]
        self.assertEqual(taxonomy_schema["date_preset"]["default"], "all_time")
        self.assertIn("last_90_days", taxonomy_schema["date_preset"]["description"])
        self.assertIn("YYYY-MM-DD", taxonomy_schema["end_date"]["description"])
        timeseries_schema = tools["get_performance_timeseries"]["inputSchema"]["properties"]
        self.assertEqual(timeseries_schema["group_by"]["default"], "ad_name")
        self.assertEqual(timeseries_schema["date_preset"]["default"], "last_30d")
        self.assertEqual(timeseries_schema["metric"]["default"], "roas")
        self.assertEqual(timeseries_schema["signal_focus"]["default"], "all")
        self.assertEqual(timeseries_schema["trajectory_focus"]["default"], "all")
        self.assertEqual(timeseries_schema["minimum_spend"]["default"], 500)
        self.assertEqual(timeseries_schema["minimum_points"]["default"], 0)
        self.assertEqual(timeseries_schema["minimum_calendar_days"]["default"], 0)
        self.assertEqual(timeseries_schema["maximum_gap_days"]["default"], 0)
        self.assertEqual(timeseries_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertIn("last_90d", timeseries_schema["date_preset"]["description"])
        self.assertIn("landing_page_domain", timeseries_schema["group_by"]["description"])
        self.assertIn("visual_style", timeseries_schema["group_by"]["description"])
        self.assertIn("cta", timeseries_schema["group_by"]["description"])
        self.assertIn("demographic_segment", timeseries_schema["group_by"]["description"])
        self.assertIn("funnel_score", timeseries_schema["metric"]["description"])
        self.assertIn("fatigued", timeseries_schema["signal_focus"]["description"])
        self.assertIn("insufficient_data", timeseries_schema["trajectory_focus"]["description"])
        self.assertIn("sync gap", timeseries_schema["maximum_gap_days"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_desc)
        demographics_schema = tools["get_demographics_performance"]["inputSchema"]["properties"]
        self.assertEqual(demographics_schema["date_preset"]["default"], "all_time")
        self.assertIn("last_30_days", demographics_schema["date_preset"]["description"])
        self.assertIn("start_date", demographics_schema)
        self.assertIn("end_date", demographics_schema)
        self.assertIn("YYYY-MM-DD", demographics_schema["start_date"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_schema["end_date"]["description"])
        self.assertIn("custom performance report", custom_desc)
        self.assertIn("dimension combinations", custom_desc)
        self.assertIn("hook x landing_page x offer_type", custom_desc)
        self.assertIn("start_date and end_date", custom_desc)
        self.assertIn(
            "landing_page",
            tools["create_custom_report"]["inputSchema"]["properties"]["dimensions"][
                "description"
            ],
        )
        custom_schema = tools["create_custom_report"]["inputSchema"]["properties"]
        self.assertIn("start_date", custom_schema)
        self.assertIn("end_date", custom_schema)
        self.assertIn("YYYY-MM-DD", custom_schema["start_date"]["description"])
        self.assertIn("YYYY-MM-DD", custom_schema["end_date"]["description"])
        self.assertIn("reusable custom report", saved_desc)
        self.assertIn("hook_type x landing_page x offer_type", saved_desc)
        self.assertIn("specific test period", saved_desc)
        self.assertIn("metric preset", saved_desc)
        saved_schema = tools["save_custom_report"]["inputSchema"]["properties"]
        self.assertEqual(saved_schema["view_type"]["default"], "table")
        self.assertIn("matrix", saved_schema["view_type"]["description"])
        self.assertEqual(saved_schema["date_range"]["default"], "last_30_days")
        self.assertIn("custom", saved_schema["date_range"]["description"])
        self.assertEqual(saved_schema["group_by"]["default"], "creative")
        self.assertIn("dimension", saved_schema["group_by"]["description"])
        self.assertIn("metrics", saved_schema)
        self.assertIn("roas", saved_schema["metrics"]["description"])
        self.assertIn("filters", saved_schema)
        self.assertIn("field/value pairs", saved_schema["filters"]["description"])
        self.assertEqual(saved_schema["sort"]["default"], "desc")
        self.assertIn("asc", saved_schema["sort"]["description"])
        self.assertIn("saved_metric_preset", saved_schema)
        self.assertIn("delivery", saved_schema["saved_metric_preset"]["description"])
        self.assertIn("start_date", saved_schema)
        self.assertIn("end_date", saved_schema)
        self.assertIn("YYYY-MM-DD", saved_schema["start_date"]["description"])
        self.assertIn("YYYY-MM-DD", saved_schema["end_date"]["description"])
        self.assertIn("video_p100", import_rows["description"])

    def test_strategy_tool_forwards_demographic_and_date_controls(self) -> None:
        source = SERVER.read_text()
        strategy_handler = source.split(
            "async def _get_creative_strategy_report(args: dict) -> list[TextContent]:",
            1,
        )[1].split("async def _get_brain_learnings", 1)[0]

        self.assertIn('"date_preset": args.get("date_preset", "all_time")', source)
        self.assertIn('"start_date": args.get("start_date", "")', source)
        self.assertIn('"end_date": args.get("end_date", "")', source)
        self.assertIn('for key in ("rows", "columns", "status_focus", "metric_preset"):', source)
        self.assertIn('metrics = _csv_arg(args.get("metrics"))', source)
        self.assertIn('params["metrics"] = metrics', source)
        self.assertIn("params = _strategy_params(args)", strategy_handler)
        self.assertIn('"roas_target"', source)
        self.assertIn('"fatigue_minimum_calendar_days"', source)
        self.assertIn('"watch_group_by"', source)
        self.assertIn('"watch_metric"', source)
        self.assertIn('"watch_signal_focus"', source)
        self.assertIn('"watch_minimum_points"', source)
        self.assertIn('"watch_minimum_calendar_days"', source)
        self.assertIn('"watch_maximum_gap_days"', source)
        self.assertIn('"watch_sources"', source)
        self.assertIn('"conclusion_statuses"', source)
        self.assertIn('"audience_signal_focus"', source)
        self.assertIn('"audience_limit"', source)
        self.assertIn('"minimum_points": args.get("minimum_points", 0)', source)
        self.assertIn('"minimum_calendar_days": args.get("minimum_calendar_days", 0)', source)
        self.assertIn('"maximum_gap_days": args.get("maximum_gap_days", 0)', source)
        self.assertIn('"fatigue_decay_threshold"', source)
        self.assertIn('"trajectory_focus": args.get("trajectory_focus", "all")', source)
        self.assertIn('"date_preset": args.get("date_preset", "all_time")', source)
        self.assertIn('"date_preset": args.get("date_preset", "last_30d")', source)
        self.assertIn('params["start_date"] = args["start_date"]', source)
        self.assertIn('params["end_date"] = args["end_date"]', source)
        self.assertIn('"watch_group_by"', source)
        self.assertIn('"watch_metric"', source)
        self.assertIn('"watch_signal_focus"', source)
        self.assertIn('"watch_trajectory_focus"', source)
        self.assertIn('"watch_minimum_points"', source)
        self.assertIn('"watch_minimum_calendar_days"', source)
        self.assertIn('"watch_maximum_gap_days"', source)
        self.assertIn('"watch_limit"', source)
        self.assertIn('f"{API_URL}/meta/performance/summary"', source)
        self.assertIn('f"{API_URL}/performance/by-taxonomy"', source)
        self.assertIn('f"{API_URL}/reports/prebuilt"', source)
        self.assertIn('f"{API_URL}/performance/demographics"', source)
        self.assertIn('if name == "get_competitor_scan_history":', source)
        self.assertIn('f"{API_URL}/competitors/history"', source)
        self.assertIn('if name == "save_brain_learnings":', source)
        self.assertIn('f"{API_URL}/brain/learnings/save"', source)
        self.assertIn('if name == "export_brain_learnings_context":', source)
        self.assertIn("async def _export_brain_learnings_context(args: dict)", source)
        self.assertIn('payload = await _get_brain_learnings(args)', source)
        self.assertIn('parsed.get("agent_context")', source)
        self.assertIn('_csv_arg(args.get(key))', source)
        self.assertIn('payload[key] = args[key]', source)
        self.assertIn('for key in ("start_date", "end_date"):', source)
        self.assertIn('f"{API_URL}/reports/custom"', source)
        self.assertIn('f"{API_URL}/reports/custom/saved"', source)
        self.assertIn('"view_type": args.get("view_type", "table")', source)
        self.assertIn('"date_range": args.get("date_range", "last_30_days")', source)
        self.assertIn('"group_by": args.get("group_by", "creative")', source)
        self.assertIn('"metrics": args.get("metrics") or []', source)
        self.assertIn('"filters": args.get("filters") or []', source)
        self.assertIn('"sort": args.get("sort", "desc")', source)
        self.assertIn('"saved_metric_preset": args.get("saved_metric_preset", "")', source)

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
        self.assertIn("get_competitor_scan_history", _declared_tools())


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
