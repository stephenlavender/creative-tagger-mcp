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
README = ROOT / "README.md"

PUBLIC_EXPECTED_TOOLS = {
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
    "save_brain_learnings",
    "export_brain_learnings_context",
    "get_performance_timeseries",
    "export_performance_timeseries_context",
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
            if name == "generate_naming":
                self.assertIn('if name == "generate_naming":', source)
            else:
                self.assertRegex(source, rf'"{name}":\s+_[a-z_]+')

    def test_package_version_matches_v2_surface(self) -> None:
        init_file = ROOT / "src" / "creative_tagger_mcp" / "__init__.py"
        self.assertIn('__version__ = "0.2.2"', init_file.read_text())
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('version = "0.2.2"', pyproject)
        self.assertIn('"mcp>=1.28.1,<2"', pyproject)

    def test_workspace_first_surface_and_brand_scopes_are_declared(self) -> None:
        tools = _declared_tools()

        self.assertIn("list_workspaces", tools)
        self.assertEqual(
            tools["list_workspaces"]["inputSchema"],
            {"type": "object", "properties": {}},
        )
        for name in (
            "list_library",
            "get_library_patterns",
            "get_analysis",
            "get_meta_status",
        ):
            self.assertIn("brand_name", tools[name]["inputSchema"]["properties"])
        library_schema = tools["list_library"]["inputSchema"]["properties"]
        self.assertEqual(library_schema["limit"]["minimum"], 1)
        self.assertEqual(library_schema["limit"]["maximum"], 100)
        self.assertEqual(library_schema["offset"]["minimum"], 0)

    def test_server_instructions_are_workspace_safe_and_causally_honest(self) -> None:
        source = SERVER.read_text()

        self.assertIn("version=__version__", source)
        self.assertIn("instructions=PLAYBOOK_INSTRUCTIONS", source)
        self.assertIn("call list_workspaces first", source)
        self.assertIn("never blend or infer across", source)
        self.assertIn("historical associations", source)
        self.assertIn("falsifiable", source)
        self.assertIn("ship/stop", source)

        predict = _declared_tools()["predict_creative"]["description"]
        self.assertIn("not a forecast", predict)
        self.assertIn("controlled-test hypothesis", predict)
        self.assertNotIn("predict how a creative will perform", predict.lower())

    def test_strategy_is_concise_by_default_with_detailed_opt_in(self) -> None:
        schema = _declared_tools()["get_creative_strategy_report"]["inputSchema"]
        props = schema["properties"]

        self.assertEqual(props["response_format"]["default"], "concise")
        self.assertEqual(props["response_format"]["enum"], ["concise", "detailed"])
        self.assertEqual(props["max_cells"]["default"], 24)
        self.assertEqual(props["max_cells"]["maximum"], 200)

        namespace = _load_pure_helpers(
            {
                "_csv_arg",
                "_infer_strategy_template",
                "_normalize_strategy_axis",
                "_strategy_params",
            }
        )
        params = namespace["_strategy_params"]({"brand_name": "Acme"})
        self.assertEqual(params["response_format"], "concise")
        self.assertEqual(params["max_cells"], 24)

    def test_readme_matches_published_surface_and_current_models(self) -> None:
        readme = README.read_text()

        self.assertIn("packaged metadata are\nversion `0.2.2`", readme)
        self.assertIn("pip install creative-tagger-mcp==0.2.2", readme)
        self.assertNotIn("pip install creative-tagger-mcp==0.2.1", readme)
        self.assertNotIn("unreleased `0.2.2` candidate", readme)
        self.assertIn("companion API must be deployed", readme)
        self.assertIn("Current chart view types are `table`, `bar`, `line`, and `pie`", readme)
        self.assertNotIn('"view_type": "matrix"', readme)
        self.assertIn("Gemini 3.5 Flash", readme)
        self.assertIn("Claude Sonnet 5", readme)
        self.assertNotIn("Gemini 2.5 Flash", readme)
        brain_docs = readme.split("### `get_brain_learnings`", 1)[1].split(
            "### `get_performance_timeseries`", 1
        )[0]
        self.assertIn("`higher_observed_efficiency`", brain_docs)
        self.assertIn("`lower_observed_efficiency`", brain_docs)
        self.assertNotIn("opportunity", brain_docs.lower())
        self.assertNotIn("waste", brain_docs.lower())

    def test_tool_copy_uses_current_taxonomy_dimension_count(self) -> None:
        source = SERVER.read_text()
        readme = README.read_text()

        self.assertIn("classification across 21 dimensions", source)
        self.assertIn("complete 21-dimension classification", source)
        self.assertNotIn("28 taxonomy dimensions", source)
        self.assertIn("21 standardized dimensions", readme)
        self.assertIn("https://api.creativetagger.ai/mcp/", readme)
        self.assertNotIn("https://api.creativetagger.ai/mcp`", readme)
        self.assertIn("15 controlled dimensions", readme)
        self.assertIn("one derived/open `aspect_ratio` dimension", readme)
        self.assertIn("`allow_other_values: true`", readme)
        self.assertIn("packaged metadata are\nversion `0.2.2`", readme)
        self.assertNotIn("PyPI still serves `creative-tagger-mcp==0.1.0`", readme)
        self.assertNotIn("28 dimensions", readme)

        tools = _declared_tools()
        analyze_description = tools["analyze_creative"]["description"]
        taxonomy_description = tools["get_taxonomy"]["description"]
        self.assertIn("media type, asset type, visual format", analyze_description)
        self.assertIn("voiceover tone", analyze_description)
        self.assertNotIn("brand presence", analyze_description)
        self.assertNotIn("social proof", analyze_description.lower())
        self.assertIn("15 controlled dimensions", taxonomy_description)
        self.assertIn("derived/open aspect-ratio dimension", taxonomy_description)

    def test_publish_workflow_verifies_release_before_upload(self) -> None:
        workflow = ROOT / ".github" / "workflows" / "publish.yml"
        source = workflow.read_text()

        self.assertIn("python -m build", source)
        self.assertIn("python scripts/smoke_release.py", source)
        self.assertIn("Verify release ref matches package version", source)
        self.assertIn('EXPECTED_TAG="v${PROJECT_VERSION}"', source)
        self.assertIn('GITHUB_REF_NAME}" != "${EXPECTED_TAG}', source)
        self.assertIn('REQUESTED_VERSION}" != "${PROJECT_VERSION}', source)
        self.assertIn("fetch-depth: 0", source)
        self.assertIn(
            "git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main",
            source,
        )
        self.assertIn('GITHUB_SHA}" != "${MAIN_SHA}', source)
        self.assertIn("Stage exact release artifact set", source)
        self.assertIn('find dist -mindepth 1 -maxdepth 1 -print', source)
        self.assertIn('test ! -L "${WHEEL}"', source)
        self.assertIn('test ! -L "${SDIST}"', source)
        self.assertIn('cp -- "${WHEEL}" "${SDIST}" release-dist/', source)
        self.assertEqual(source.count("packages-dir: release-dist/"), 2)
        self.assertNotIn("python -m twine check dist/*", source)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", source)
        self.assertIn("id-token: write", source)
        self.assertIn("PYPI_API_TOKEN", source)

    def test_local_release_upload_is_scoped_to_exact_version_artifacts(self) -> None:
        readme = README.read_text()

        self.assertNotIn("python -m twine upload dist/*", readme)
        self.assertIn(
            "dist/creative_tagger_mcp-0.2.2-py3-none-any.whl", readme
        )
        self.assertIn("dist/creative_tagger_mcp-0.2.2.tar.gz", readme)
        self.assertIn("never publish with\n`twine upload dist/*`", readme)

    def test_release_smoke_does_not_require_tomli_on_old_python(self) -> None:
        smoke = ROOT / "scripts" / "smoke_release.py"
        source = smoke.read_text()

        self.assertIn("tomllib = None", source)
        self.assertIn("_project_version", source)
        self.assertNotIn("import tomli", source)

    def test_release_smoke_materializes_entry_points_for_python_312(self) -> None:
        smoke = ROOT / "scripts" / "smoke_release.py"
        source = smoke.read_text()

        # Python 3.12's EntryPoints integer indexing is a name lookup, not a
        # sequence lookup. Materializing the selected collection keeps the
        # release assertion portable across every supported Python version.
        self.assertIn("entry_points = list(metadata.entry_points().select(", source)

    def test_release_smoke_checks_server_version_and_packaged_readme(self) -> None:
        smoke = ROOT / "scripts" / "smoke_release.py"
        source = smoke.read_text()

        self.assertIn("initialization.server_version", source)
        self.assertIn('requirement.startswith("mcp<2,>=1.28.1")', source)
        self.assertIn("package_metadata.get_payload()", source)
        self.assertIn("call list_workspaces first", source)
        self.assertIn('"packaged metadata are"', source)
        self.assertIn('"version `0.2.2`"', source)
        self.assertIn('"pip install creative-tagger-mcp==0.2.2"', source)
        self.assertIn('"pip install creative-tagger-mcp==0.2.1" not in readme', source)
        self.assertIn("len(tool_catalog) < 40_000", source)
        self.assertIn('strategy_schema["response_format"]["default"] == "concise"', source)

    def test_release_smoke_rejects_generated_paths_in_sdist(self) -> None:
        smoke = ROOT / "scripts" / "smoke_release.py"
        source = smoke.read_text()

        self.assertIn("_verify_sdist_contents(sdist)", source)
        self.assertIn('".release-smoke-venv"', source)
        self.assertIn('part.startswith(".venv")', source)
        self.assertIn("Forbidden build/environment path in sdist", source)

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

    def test_string_list_arg_normalizes_meta_window_values(self) -> None:
        namespace = _load_pure_helpers({"_string_list_arg"})
        normalize = namespace["_string_list_arg"]

        self.assertIsNone(normalize(None))
        self.assertEqual(normalize("7d_click, 1d_view"), ["7d_click", "1d_view"])
        self.assertEqual(
            normalize(["7d_click", " 1d_view ", "", None]),
            ["7d_click", "1d_view"],
        )
        self.assertEqual(normalize(""), None)

    def test_strategy_params_preserve_template_defaults_unless_overridden(self) -> None:
        namespace = _load_pure_helpers(
            {
                "_csv_arg",
                "_infer_strategy_template",
                "_normalize_strategy_axis",
                "_strategy_params",
            }
        )
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

    def test_normalize_strategy_axis_follows_taxonomy_v2_dimension_split(self) -> None:
        namespace = _load_pure_helpers({"_normalize_strategy_axis"})
        normalize = namespace["_normalize_strategy_axis"]

        # Taxonomy v2: asset_type (production class), media_type (auto-detected
        # format), and product are distinct canonical axes — the pre-v2
        # normalizer collapsed asset_type into ad_type, silently swapping a
        # different dimension into the visual-format axis.
        self.assertEqual(normalize("asset_type"), "asset_type")
        self.assertEqual(normalize("media_type"), "media_type")
        self.assertEqual(normalize("product"), "product")

        # visual_format is the canonical execution-style key; it resolves to
        # the API's deprecated ad_type alias (identical sources) because this
        # normalizer also feeds the watch/timeseries space, which still keys
        # on ad_type. Legacy spellings resolve the same way.
        self.assertEqual(normalize("visual_format"), "ad_type")
        self.assertEqual(normalize("creative_type"), "ad_type")
        self.assertEqual(normalize("ad_type"), "ad_type")

        self.assertEqual(normalize("angle"), "messaging_angle")
        self.assertEqual(normalize("hook_type"), "hook")

    def test_strategy_params_normalize_template_aliases(self) -> None:
        namespace = _load_pure_helpers(
            {
                "_csv_arg",
                "_infer_strategy_template",
                "_normalize_strategy_axis",
                "_strategy_params",
            }
        )
        strategy_params = namespace["_strategy_params"]

        self.assertEqual(
            strategy_params({"brand_name": "Acme", "report_template": "winners"})["report_template"],
            "creative-winners",
        )
        self.assertEqual(
            strategy_params({"brand_name": "Acme", "report_template": "fatigue"})["report_template"],
            "fatigue-watch",
        )
        self.assertEqual(
            strategy_params({"brand_name": "Acme", "report_template": "coverage"})["report_template"],
            "coverage-gaps",
        )
        self.assertEqual(
            strategy_params({"brand_name": "Acme", "report_template": "personas"})["report_template"],
            "persona-read",
        )
        self.assertEqual(
            strategy_params({"brand_name": "Acme", "report_template": "angle audience"})["report_template"],
            "angle-audience-fit",
        )

    def test_strategy_params_infer_audience_templates_from_axes(self) -> None:
        namespace = _load_pure_helpers(
            {
                "_csv_arg",
                "_infer_strategy_template",
                "_normalize_strategy_axis",
                "_strategy_params",
            }
        )
        strategy_params = namespace["_strategy_params"]

        demographic = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "demographic_age",
                "columns": "demographic_gender",
            }
        )
        self.assertEqual(demographic["report_template"], "demographic-read")

        audience_signals = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "demographic_signal",
                "columns": "demographic_segment",
            }
        )
        self.assertEqual(audience_signals["report_template"], "audience-signals")

        mixed = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "messaging_angle",
                "columns": "demographic_segment",
            }
        )
        self.assertEqual(mixed["report_template"], "angle-audience-fit")

        demographic_aliases = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "age",
                "columns": "gender",
            }
        )
        self.assertEqual(demographic_aliases["report_template"], "demographic-read")

        audience_signal_aliases = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "signal",
                "columns": "segment",
            }
        )
        self.assertEqual(audience_signal_aliases["report_template"], "audience-signals")

        mixed_aliases = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "hook_type",
                "columns": "audience_segment",
            }
        )
        self.assertEqual(mixed_aliases["report_template"], "hook-audience-fit")

        hook_mixed = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "hook",
                "columns": "demographic_segment",
            }
        )
        self.assertEqual(hook_mixed["report_template"], "hook-audience-fit")

        unmapped_mixed = strategy_params(
            {
                "brand_name": "Acme",
                "rows": "ad_type",
                "columns": "demographic_segment",
            }
        )
        self.assertNotIn("report_template", unmapped_mixed)

        creative_only = strategy_params(
            {"brand_name": "Acme", "rows": "messaging_angle", "columns": "ad_type"}
        )
        self.assertNotIn("report_template", creative_only)

    def test_demographics_export_helpers_build_decision_queue_and_strategy_views(self) -> None:
        namespace = _load_pure_helpers(
            {
                "_demographic_segment_label",
                "_compact_demographic_segment",
                "_format_demographic_evidence",
                "_build_demographics_decision_queue",
                "_build_demographics_strategy_query",
                "_build_demographics_strategy_views",
                "_build_demographic_timeseries_query",
                "_build_demographic_timeseries_views",
            }
        )

        queue = namespace["_build_demographics_decision_queue"](
            [
                {
                    "age": "25-34",
                    "gender": "female",
                    "observed_efficiency_band": "higher_observed_return_per_spend",
                    "return_per_spend_percentile": 100,
                    "spend": 1250,
                    "roas": 4.2,
                    "ctr": 2.7,
                    "cpa": 32.5,
                    "conversions": 18,
                }
            ],
            [
                {
                    "age": "45-54",
                    "gender": "male",
                    "observed_efficiency_band": "lower_observed_return_per_spend",
                    "return_per_spend_percentile": 0,
                    "spend": 980,
                    "roas": 0.9,
                    "ctr": 0.8,
                    "cpa": 140,
                    "conversions": 7,
                }
            ],
            limit=3,
        )

        self.assertEqual(len(queue), 2)
        self.assertEqual(queue[0]["rank"], 1)
        self.assertEqual(queue[0]["action"], "review_observed_delivery")
        self.assertIn("25-34 / female", queue[0]["recommendation"])
        self.assertIn("$1250 spend", queue[0]["evidence_summary"])
        self.assertFalse(queue[0]["causal_claim"])
        self.assertEqual(
            queue[0]["observation_plan"]["interpretation"],
            "association_not_causation",
        )
        self.assertNotIn("controlled_test", queue[0])
        self.assertEqual(queue[1]["action"], "review_observed_delivery")
        self.assertIn("45-54 / male", queue[1]["recommendation"])
        self.assertIn("0.90x ROAS", queue[1]["evidence_summary"])
        self.assertNotIn("opportunity", json.dumps(queue).lower())
        self.assertNotIn("waste", json.dumps(queue).lower())

        views = namespace["_build_demographics_strategy_views"](
            brand_name="Acme",
            date_preset="custom",
            start_date="2026-05-01",
            end_date="2026-05-31",
        )
        self.assertEqual(views[0]["report_template"], "demographic-read")
        self.assertEqual(views[0]["strategy_query"]["tool"], "get_creative_strategy_report")
        self.assertEqual(views[0]["strategy_query"]["brand_name"], "Acme")
        self.assertEqual(views[0]["strategy_query"]["date_preset"], "custom")
        self.assertEqual(views[0]["strategy_query"]["start_date"], "2026-05-01")
        self.assertEqual(views[0]["strategy_query"]["end_date"], "2026-05-31")
        self.assertIn("roas", views[0]["strategy_query"]["metrics"])
        self.assertEqual(views[1]["label"], "Audience signals")
        self.assertEqual(views[1]["rows"], "demographic_segment")
        self.assertEqual(views[1]["columns"], "demographic_signal")
        self.assertEqual(views[1]["report_template"], "audience-signals")
        self.assertEqual(views[2]["rows"], "messaging_angle")
        self.assertEqual(views[2]["columns"], "demographic_segment")
        self.assertEqual(views[2]["report_template"], "angle-audience-fit")
        self.assertEqual(views[3]["rows"], "hook")
        self.assertEqual(views[3]["report_template"], "hook-audience-fit")
        self.assertEqual(views[3]["fill_metric"], "hook_rate")
        self.assertIn("hook_rate", views[3]["strategy_query"]["metrics"])

        timeseries_views = namespace["_build_demographic_timeseries_views"](
            brand_name="Acme",
            date_preset="custom",
            start_date="2026-05-01",
            end_date="2026-05-31",
        )
        self.assertEqual(len(timeseries_views), 2)
        self.assertEqual(timeseries_views[0]["label"], "Audience trend watch")
        self.assertEqual(timeseries_views[0]["timeseries_query"]["tool"], "get_performance_timeseries")
        self.assertEqual(timeseries_views[0]["timeseries_query"]["brand_name"], "Acme")
        self.assertEqual(timeseries_views[0]["timeseries_query"]["group_by"], "demographic_segment")
        self.assertEqual(timeseries_views[0]["timeseries_query"]["date_preset"], "custom")
        self.assertEqual(timeseries_views[1]["timeseries_query"]["group_by"], "demographic_signal")

    def test_brain_export_helpers_build_strategy_queries_and_queue(self) -> None:
        namespace = _load_pure_helpers(
            {
                "_normalize_strategy_axis",
                "_build_demographics_strategy_query",
                "_brain_learning_status_action",
                "_brain_learning_strategy_query",
                "_brain_learning_timeseries_query",
                "_build_brain_learning_decision_queue",
                "_build_brain_learning_strategy_views",
                "_build_brain_learning_timeseries_views",
            }
        )

        learnings = [
            {
                "id": "working:hook_type:question",
                "kind": "working",
                "title": "Question is a proven hook type",
                "summary": "Question is clearing the account benchmark.",
                "action": "Keep Question fixed and iterate the adjacent hook, offer, or format.",
                "evidence": {"dimension": "hook_type", "value": "Question"},
            },
            {
                "id": "watch:demographic_segment:25-34-female",
                "kind": "watch",
                "title": "25-34 / female is fatiguing",
                "summary": "This audience slice is weakening.",
                "action": "Refresh this segment before adding more spend.",
                "evidence": {"dimension": "demographic_segment", "value": "25-34 / female"},
                "source": "timeseries",
            },
            {
                "id": "conclusion:winner:founder-proof",
                "kind": "conclusion",
                "title": "Founder proof just cleared learning as a winner",
                "summary": "Founder proof moved from learning to winner.",
                "action": "Use founder proof as the control and brief adjacent variants.",
                "evidence": {"dimension": "creative_conclusion", "value": "Founder proof", "current_status": "winner"},
            },
            {
                "id": "gap:hook_type:contrarian",
                "kind": "gap",
                "title": "Contrarian is still untested",
                "summary": "No measured creative has tested Contrarian yet.",
                "action": "Brief one controlled test that isolates Contrarian.",
                "evidence": {"dimension": "hook_type", "value": "Contrarian"},
            },
        ]

        queue = namespace["_build_brain_learning_decision_queue"](
            learnings=learnings,
            brand_name="Acme",
            date_preset="custom",
            start_date="2026-05-01",
            end_date="2026-05-31",
            watch_metric="roas",
            watch_signal_focus="fatigued",
            watch_trajectory_focus="worsening",
            watch_coverage_focus="gappy",
            watch_minimum_points=3,
            watch_minimum_calendar_days=7,
            watch_maximum_gap_days=4,
            fatigue_decay_threshold=0.24,
            limit=4,
        )
        self.assertEqual([item["rank"] for item in queue], [1, 2, 3, 4])
        self.assertEqual(queue[0]["action"], "validate")
        self.assertEqual(queue[0]["strategy_query"]["report_template"], "hook-performance")
        self.assertEqual(queue[0]["strategy_query"]["rows"], "hook")
        self.assertEqual(queue[1]["action"], "investigate")
        self.assertEqual(queue[1]["strategy_query"]["report_template"], "angle-audience-fit")
        self.assertEqual(queue[1]["strategy_query"]["columns"], "demographic_segment")
        self.assertEqual(queue[1]["timeseries_query"]["tool"], "get_performance_timeseries")
        self.assertEqual(queue[1]["timeseries_query"]["group_by"], "demographic_segment")
        self.assertEqual(queue[1]["timeseries_query"]["signal_focus"], "fatigued")
        self.assertEqual(queue[1]["timeseries_query"]["coverage_focus"], "gappy")
        self.assertEqual(queue[1]["timeseries_query"]["focus_value"], "25-34 / female")
        self.assertEqual(queue[2]["action"], "validate")
        self.assertEqual(queue[2]["strategy_query"]["report_template"], "creative-winners")
        self.assertEqual(queue[2]["strategy_query"]["focus_status"], "winner")
        self.assertEqual(queue[3]["action"], "test")
        self.assertEqual(queue[3]["strategy_query"]["report_template"], "coverage-gaps")
        self.assertEqual(queue[3]["strategy_query"]["date_preset"], "custom")
        self.assertEqual(queue[3]["strategy_query"]["start_date"], "2026-05-01")
        self.assertEqual(queue[3]["strategy_query"]["end_date"], "2026-05-31")

        views = namespace["_build_brain_learning_strategy_views"](
            learnings=learnings,
            brand_name="Acme",
            date_preset="custom",
            start_date="2026-05-01",
            end_date="2026-05-31",
            limit=2,
        )
        self.assertEqual(len(views), 2)
        self.assertEqual(views[0]["learning_id"], "working:hook_type:question")
        self.assertEqual(views[0]["strategy_query"]["focus_value"], "Question")
        self.assertEqual(views[1]["strategy_query"]["report_template"], "angle-audience-fit")

        timeseries_views = namespace["_build_brain_learning_timeseries_views"](
            learnings=learnings,
            brand_name="Acme",
            date_preset="custom",
            start_date="2026-05-01",
            end_date="2026-05-31",
            watch_metric="roas",
            watch_signal_focus="fatigued",
            watch_trajectory_focus="worsening",
            watch_coverage_focus="gappy",
            watch_minimum_points=3,
            watch_minimum_calendar_days=7,
            watch_maximum_gap_days=4,
            fatigue_decay_threshold=0.24,
            limit=4,
        )
        self.assertEqual(len(timeseries_views), 1)
        self.assertEqual(timeseries_views[0]["learning_id"], "watch:demographic_segment:25-34-female")
        self.assertEqual(timeseries_views[0]["timeseries_query"]["group_by"], "demographic_segment")
        self.assertEqual(timeseries_views[0]["timeseries_query"]["maximum_gap_days"], 4)
        self.assertEqual(timeseries_views[0]["timeseries_query"]["fatigue_decay_threshold"], 0.24)

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
        meta_sync_desc = tools["sync_meta_performance"]["description"]
        strategy_desc = tools["get_creative_strategy_report"]["description"]
        brain_desc = tools["get_brain_learnings"]["description"]
        brain_save_desc = tools["save_brain_learnings"]["description"]
        brain_export_desc = tools["export_brain_learnings_context"]["description"]
        timeseries_desc = tools["get_performance_timeseries"]["description"]
        timeseries_export_desc = tools["export_performance_timeseries_context"]["description"]
        demographics_desc = tools["get_demographics_performance"]["description"]
        demographics_export_desc = tools["export_demographics_context"]["description"]
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
        self.assertIn("attribution", meta_sync_desc)
        meta_sync_schema = tools["sync_meta_performance"]["inputSchema"]["properties"]
        self.assertIn("attribution_windows", meta_sync_schema)
        self.assertEqual(meta_sync_schema["attribution_windows"]["type"], "array")
        self.assertIn("7d_click", meta_sync_schema["attribution_windows"]["description"])
        self.assertIn("1d_view", meta_sync_schema["attribution_windows"]["description"])
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
        self.assertEqual(strategy_schema["watch_coverage_focus"]["default"], "all")
        self.assertEqual(strategy_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(strategy_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(strategy_schema["watch_limit"]["default"], 5)
        self.assertIn("messaging_angle", strategy_schema["watch_group_by"]["description"])
        self.assertIn("demographic_segment", strategy_schema["watch_group_by"]["description"])
        self.assertIn("thumbstop_rate", strategy_schema["watch_metric"]["description"])
        self.assertIn("fatigued", strategy_schema["watch_signal_focus"]["description"])
        self.assertIn("worsening", strategy_schema["watch_trajectory_focus"]["description"])
        self.assertIn("windowed_history", strategy_schema["watch_coverage_focus"]["description"])
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
        self.assertIn("audience efficiency observations", brain_desc)
        self.assertIn("conclusion-only", brain_desc)
        self.assertIn("working-only", brain_desc)
        self.assertIn("demographic_segment", brain_desc)
        self.assertIn("lower-observed-efficiency", brain_desc)
        self.assertIn("watch_coverage_focus", brain_desc)
        self.assertIn("windowed-history", brain_desc)
        self.assertIn("Persist", brain_save_desc)
        self.assertIn("Brand Brain notes", brain_save_desc)
        self.assertIn("demographic_signal", brain_save_desc)
        self.assertIn("higher-", brain_save_desc)
        self.assertIn("lower-observed-efficiency", brain_save_desc)
        self.assertIn("watch_coverage_focus", brain_save_desc)
        self.assertIn("windowed-history", brain_save_desc)
        self.assertIn("agent_context payload", brain_export_desc)
        self.assertIn("brief-ready prompt seed", brain_export_desc)
        self.assertIn("saved Brand Brain context", brain_export_desc)
        self.assertIn("time-series follow-up queries", brain_export_desc)
        self.assertIn("watch_coverage_focus", brain_export_desc)
        self.assertIn("strategy queries", brain_export_desc)
        brain_export_schema = tools["export_brain_learnings_context"]["inputSchema"]["properties"]
        self.assertEqual(brain_export_schema["limit"]["default"], 8)
        self.assertEqual(brain_export_schema["date_preset"]["default"], "all_time")
        self.assertIn("YYYY-MM-DD", brain_export_schema["start_date"]["description"])
        self.assertEqual(brain_export_schema["watch_group_by"]["default"], "messaging_angle")
        self.assertEqual(brain_export_schema["watch_metric"]["default"], "roas")
        self.assertEqual(brain_export_schema["watch_signal_focus"]["default"], "all")
        self.assertEqual(brain_export_schema["watch_trajectory_focus"]["default"], "all")
        self.assertEqual(brain_export_schema["watch_coverage_focus"]["default"], "all")
        self.assertEqual(brain_export_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_export_schema["watch_minimum_calendar_days"]["default"], 0)
        self.assertEqual(brain_export_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(brain_export_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(brain_export_schema["audience_signal_focus"]["default"], "all")
        self.assertEqual(brain_export_schema["audience_limit"]["default"], 3)
        self.assertEqual(brain_export_schema["conclusion_recency_days"]["default"], 0)
        self.assertIn("conclusion, working, watch, audience, gap", brain_export_schema["kinds"]["description"])
        self.assertIn(
            "report end date",
            brain_export_schema["conclusion_recency_days"]["description"],
        )
        self.assertIn("demographic_segment", brain_export_schema["watch_group_by"]["description"])
        self.assertIn("thumbstop_rate", brain_export_schema["watch_metric"]["description"])
        self.assertIn("hook_rate", brain_export_schema["watch_metric"]["description"])
        self.assertIn("hold_rate", brain_export_schema["watch_metric"]["description"])
        self.assertIn("frequency", brain_export_schema["watch_metric"]["description"])
        self.assertIn("outbound_ctr", brain_export_schema["watch_metric"]["description"])
        self.assertIn("landing_page_views", brain_export_schema["watch_metric"]["description"])
        self.assertIn("adds_to_cart", brain_export_schema["watch_metric"]["description"])
        self.assertIn("atc_per_lpv", brain_export_schema["watch_metric"]["description"])
        self.assertIn("video_3s_views", brain_export_schema["watch_metric"]["description"])
        self.assertIn("worsening", brain_export_schema["watch_trajectory_focus"]["description"])
        self.assertIn("insufficient_points", brain_export_schema["watch_coverage_focus"]["description"])
        self.assertIn("strategy", brain_export_schema["watch_sources"]["description"])
        expected_audience_focus = [
            "all",
            "higher_observed_efficiency",
            "lower_observed_efficiency",
        ]
        self.assertEqual(
            brain_export_schema["audience_signal_focus"]["enum"],
            expected_audience_focus,
        )
        self.assertIn(
            "lower_observed_efficiency",
            brain_export_schema["audience_signal_focus"]["description"],
        )
        brain_save_schema = tools["save_brain_learnings"]["inputSchema"]["properties"]
        self.assertEqual(brain_save_schema["limit"]["default"], 8)
        self.assertEqual(brain_save_schema["include_gaps_in_notes"]["default"], False)
        self.assertEqual(brain_save_schema["date_preset"]["default"], "all_time")
        self.assertIn("YYYY-MM-DD", brain_save_schema["start_date"]["description"])
        self.assertEqual(brain_save_schema["watch_group_by"]["default"], "messaging_angle")
        self.assertEqual(brain_save_schema["watch_metric"]["default"], "roas")
        self.assertEqual(brain_save_schema["watch_signal_focus"]["default"], "all")
        self.assertEqual(brain_save_schema["watch_trajectory_focus"]["default"], "all")
        self.assertEqual(brain_save_schema["watch_coverage_focus"]["default"], "all")
        self.assertEqual(brain_save_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_save_schema["watch_minimum_calendar_days"]["default"], 0)
        self.assertEqual(brain_save_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(brain_save_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(brain_save_schema["audience_signal_focus"]["default"], "all")
        self.assertEqual(brain_save_schema["audience_limit"]["default"], 3)
        self.assertEqual(brain_save_schema["conclusion_recency_days"]["default"], 0)
        self.assertIn("winner", brain_save_schema["conclusion_statuses"]["description"])
        self.assertIn("fatigued", brain_save_schema["conclusion_statuses"]["description"])
        self.assertIn(
            "report end date",
            brain_save_schema["conclusion_recency_days"]["description"],
        )
        self.assertEqual(
            brain_save_schema["audience_signal_focus"]["enum"],
            expected_audience_focus,
        )
        self.assertIn(
            "higher_observed_efficiency",
            brain_save_schema["audience_signal_focus"]["description"],
        )
        self.assertIn("strategy", brain_save_schema["watch_sources"]["description"])
        self.assertIn("fatigued", brain_save_schema["watch_signal_focus"]["description"])
        self.assertIn("worsening", brain_save_schema["watch_trajectory_focus"]["description"])
        self.assertIn("short_window", brain_save_schema["watch_coverage_focus"]["description"])
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
        self.assertEqual(brain_schema["watch_coverage_focus"]["default"], "all")
        self.assertEqual(brain_schema["watch_minimum_points"]["default"], 2)
        self.assertEqual(brain_schema["watch_minimum_calendar_days"]["default"], 0)
        self.assertEqual(brain_schema["watch_maximum_gap_days"]["default"], 0)
        self.assertEqual(brain_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(brain_schema["audience_signal_focus"]["default"], "all")
        self.assertEqual(brain_schema["audience_limit"]["default"], 3)
        self.assertEqual(brain_schema["conclusion_recency_days"]["default"], 0)
        self.assertIn("loser", brain_schema["conclusion_statuses"]["description"])
        self.assertIn("all", brain_schema["conclusion_statuses"]["description"])
        self.assertIn("report end date", brain_schema["conclusion_recency_days"]["description"])
        self.assertEqual(
            brain_schema["audience_signal_focus"]["enum"],
            expected_audience_focus,
        )
        self.assertIn(
            "lower_observed_efficiency",
            brain_schema["audience_signal_focus"]["description"],
        )
        self.assertIn("visual_style", brain_schema["watch_group_by"]["description"])
        self.assertIn("demographic_age", brain_schema["watch_group_by"]["description"])
        self.assertIn("demographic_signal", brain_save_schema["watch_group_by"]["description"])
        self.assertIn("thumbstop_rate", brain_schema["watch_metric"]["description"])
        self.assertIn("hook_rate", brain_schema["watch_metric"]["description"])
        self.assertIn("hold_rate", brain_schema["watch_metric"]["description"])
        self.assertIn("frequency", brain_schema["watch_metric"]["description"])
        self.assertIn("outbound_ctr", brain_schema["watch_metric"]["description"])
        self.assertIn("landing_page_views", brain_schema["watch_metric"]["description"])
        self.assertIn("adds_to_cart", brain_schema["watch_metric"]["description"])
        self.assertIn("atc_per_lpv", brain_schema["watch_metric"]["description"])
        self.assertIn("video_3s_views", brain_schema["watch_metric"]["description"])
        self.assertIn("stable", brain_schema["watch_signal_focus"]["description"])
        self.assertIn("improving", brain_schema["watch_trajectory_focus"]["description"])
        self.assertIn("call_ready", brain_schema["watch_coverage_focus"]["description"])
        self.assertIn("sync gap", brain_schema["watch_maximum_gap_days"]["description"])
        self.assertIn("timeseries", brain_schema["watch_sources"]["description"])
        self.assertIn("patterns", brain_schema["watch_sources"]["description"])
        self.assertIn("fatigue", timeseries_desc)
        self.assertIn("mid-funnel", timeseries_desc)
        self.assertIn("delivery", timeseries_desc)
        self.assertIn("thumbstop", timeseries_desc)
        self.assertIn("analysis id", timeseries_desc)
        self.assertIn("audience slice", timeseries_desc)
        self.assertIn("visual style", timeseries_desc)
        self.assertIn("worsening", timeseries_desc)
        self.assertIn("windowed-history", timeseries_desc)
        self.assertIn("agent_context payload", timeseries_export_desc)
        self.assertIn("decision queue", timeseries_export_desc)
        self.assertIn("validate, hold, or sync more data", timeseries_export_desc)
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
        self.assertEqual(timeseries_schema["coverage_focus"]["default"], "all")
        self.assertEqual(timeseries_schema["minimum_spend"]["default"], 500)
        self.assertEqual(timeseries_schema["minimum_points"]["default"], 0)
        self.assertEqual(timeseries_schema["minimum_calendar_days"]["default"], 0)
        self.assertEqual(timeseries_schema["maximum_gap_days"]["default"], 0)
        self.assertEqual(timeseries_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(timeseries_schema["limit"]["minimum"], 1)
        self.assertEqual(timeseries_schema["limit"]["maximum"], 10)
        self.assertIn("last_90d", timeseries_schema["date_preset"]["description"])
        self.assertIn("landing_page_domain", timeseries_schema["group_by"]["description"])
        self.assertIn("visual_style", timeseries_schema["group_by"]["description"])
        self.assertIn("cta", timeseries_schema["group_by"]["description"])
        self.assertIn("demographic_segment", timeseries_schema["group_by"]["description"])
        self.assertIn("funnel_score", timeseries_schema["metric"]["description"])
        self.assertIn("hook_rate", timeseries_schema["metric"]["description"])
        self.assertIn("hold_rate", timeseries_schema["metric"]["description"])
        self.assertIn("frequency", timeseries_schema["metric"]["description"])
        self.assertIn("outbound_ctr", timeseries_schema["metric"]["description"])
        self.assertIn("landing_page_views", timeseries_schema["metric"]["description"])
        self.assertIn("adds_to_cart", timeseries_schema["metric"]["description"])
        self.assertIn("atc_per_lpv", timeseries_schema["metric"]["description"])
        self.assertIn("video_3s_views", timeseries_schema["metric"]["description"])
        self.assertIn("fatigued", timeseries_schema["signal_focus"]["description"])
        self.assertIn("insufficient_data", timeseries_schema["trajectory_focus"]["description"])
        self.assertIn("windowed_history", timeseries_schema["coverage_focus"]["description"])
        self.assertIn("sync gap", timeseries_schema["maximum_gap_days"]["description"])
        timeseries_export_schema = tools["export_performance_timeseries_context"]["inputSchema"]["properties"]
        self.assertEqual(timeseries_export_schema["group_by"]["default"], "ad_name")
        self.assertEqual(timeseries_export_schema["date_preset"]["default"], "last_30d")
        self.assertEqual(timeseries_export_schema["metric"]["default"], "roas")
        self.assertEqual(timeseries_export_schema["signal_focus"]["default"], "all")
        self.assertEqual(timeseries_export_schema["trajectory_focus"]["default"], "all")
        self.assertEqual(timeseries_export_schema["coverage_focus"]["default"], "all")
        self.assertEqual(timeseries_export_schema["minimum_spend"]["default"], 500)
        self.assertEqual(timeseries_export_schema["minimum_points"]["default"], 0)
        self.assertEqual(timeseries_export_schema["minimum_calendar_days"]["default"], 0)
        self.assertEqual(timeseries_export_schema["maximum_gap_days"]["default"], 0)
        self.assertEqual(timeseries_export_schema["fatigue_decay_threshold"]["default"], 0.18)
        self.assertEqual(timeseries_export_schema["limit"]["minimum"], 1)
        self.assertEqual(timeseries_export_schema["limit"]["maximum"], 10)
        self.assertIn("demographic_segment", timeseries_export_schema["group_by"]["description"])
        self.assertIn("funnel_score", timeseries_export_schema["metric"]["description"])
        self.assertIn("hook_rate", timeseries_export_schema["metric"]["description"])
        self.assertIn("hold_rate", timeseries_export_schema["metric"]["description"])
        self.assertIn("frequency", timeseries_export_schema["metric"]["description"])
        self.assertIn("outbound_ctr", timeseries_export_schema["metric"]["description"])
        self.assertIn("landing_page_views", timeseries_export_schema["metric"]["description"])
        self.assertIn("adds_to_cart", timeseries_export_schema["metric"]["description"])
        self.assertIn("atc_per_lpv", timeseries_export_schema["metric"]["description"])
        self.assertIn("video_3s_views", timeseries_export_schema["metric"]["description"])
        self.assertIn("windowed_history", timeseries_export_schema["coverage_focus"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_desc)
        self.assertIn("agent-ready audience context payload", demographics_export_desc)
        self.assertIn("higher and lower observed-efficiency bands", demographics_export_desc)
        self.assertNotIn("opportunity", demographics_export_desc.lower())
        self.assertNotIn("waste", demographics_export_desc.lower())
        self.assertIn("mixed creative x audience strategy queries", demographics_export_desc)
        self.assertIn("time-series follow-up queries", demographics_export_desc)
        demographics_schema = tools["get_demographics_performance"]["inputSchema"]["properties"]
        self.assertEqual(demographics_schema["date_preset"]["default"], "all_time")
        self.assertIn("last_30_days", demographics_schema["date_preset"]["description"])
        self.assertIn("start_date", demographics_schema)
        self.assertIn("end_date", demographics_schema)
        self.assertIn("YYYY-MM-DD", demographics_schema["start_date"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_schema["end_date"]["description"])
        demographics_export_schema = tools["export_demographics_context"]["inputSchema"]["properties"]
        self.assertEqual(demographics_export_schema["date_preset"]["default"], "all_time")
        self.assertEqual(demographics_export_schema["limit"]["default"], 3)
        self.assertEqual(demographics_export_schema["limit"]["minimum"], 1)
        self.assertEqual(demographics_export_schema["limit"]["maximum"], 100)
        self.assertIn("last_30_days", demographics_export_schema["date_preset"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_export_schema["start_date"]["description"])
        self.assertIn("YYYY-MM-DD", demographics_export_schema["end_date"]["description"])
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
        self.assertIn('"response_format": args.get("response_format", "concise")', source)
        self.assertIn('"max_cells": args.get("max_cells", 24)', source)
        self.assertIn("params = _strategy_params(args)", strategy_handler)
        self.assertIn('"roas_target"', source)
        self.assertIn('"fatigue_minimum_calendar_days"', source)
        self.assertIn('"watch_group_by"', source)
        self.assertIn('"watch_metric"', source)
        self.assertIn('"watch_signal_focus"', source)
        self.assertIn('"watch_coverage_focus"', source)
        self.assertIn('"watch_trajectory_focus"', source)
        self.assertIn('"watch_minimum_points"', source)
        self.assertIn('"watch_minimum_calendar_days"', source)
        self.assertIn('"watch_maximum_gap_days"', source)
        self.assertIn('"watch_sources"', source)
        self.assertIn('"conclusion_statuses"', source)
        self.assertIn('"conclusion_recency_days"', source)
        self.assertIn('"audience_signal_focus"', source)
        self.assertIn('"audience_limit"', source)
        self.assertIn('"minimum_points": args.get("minimum_points", 0)', source)
        self.assertIn('"minimum_calendar_days": args.get("minimum_calendar_days", 0)', source)
        self.assertIn('"maximum_gap_days": args.get("maximum_gap_days", 0)', source)
        self.assertIn('"fatigue_decay_threshold"', source)
        self.assertIn('"trajectory_focus": args.get("trajectory_focus", "all")', source)
        self.assertIn('"coverage_focus": args.get("coverage_focus", "all")', source)
        self.assertIn('"date_preset": args.get("date_preset", "all_time")', source)
        self.assertIn('"date_preset": args.get("date_preset", "last_30d")', source)
        self.assertIn('"attribution_windows": _string_list_arg(args.get("attribution_windows"))', source)
        self.assertIn('params["start_date"] = args["start_date"]', source)
        self.assertIn('params["end_date"] = args["end_date"]', source)
        self.assertIn('"watch_group_by"', source)
        self.assertIn('"watch_metric"', source)
        self.assertIn('"watch_signal_focus"', source)
        self.assertIn('"watch_trajectory_focus"', source)
        self.assertIn('"watch_coverage_focus"', source)
        self.assertIn('"watch_minimum_points"', source)
        self.assertIn('"watch_minimum_calendar_days"', source)
        self.assertIn('"watch_maximum_gap_days"', source)
        self.assertIn('"watch_limit"', source)
        self.assertIn('f"{API_URL}/meta/performance/summary"', source)
        self.assertIn('f"{API_URL}/performance/by-taxonomy"', source)
        self.assertIn('f"{API_URL}/reports/prebuilt"', source)
        self.assertIn('f"{API_URL}/performance/demographics"', source)
        self.assertIn('"export_demographics_context": _export_demographics_context', source)
        self.assertIn("async def _export_demographics_context(args: dict)", source)
        self.assertIn('payload = await _get_demographics_performance(args)', source)
        self.assertIn('parsed.get("higher_observed_efficiency")', source)
        self.assertIn('parsed.get("lower_observed_efficiency")', source)
        self.assertIn('"decision_queue": decision_queue', source)
        self.assertIn('"segment_strategy_views": {', source)
        self.assertIn('"segment_timeseries_views": {', source)
        self.assertIn('query["focus_segment"] = compact["segment"]', source)
        self.assertIn('"suggested_strategy_views": _build_demographics_strategy_views(', source)
        self.assertIn('"suggested_timeseries_views": _build_demographic_timeseries_views(', source)
        self.assertIn('"strategy_query"] = _build_demographics_strategy_query(', source)
        self.assertIn('"tool": "export_demographics_context"', source)
        self.assertIn('"get_competitor_scan_history": _get_competitor_scan_history', source)
        self.assertIn('f"{API_URL}/competitors/history"', source)
        self.assertIn('"save_brain_learnings": _save_brain_learnings', source)
        self.assertIn('f"{API_URL}/brain/learnings/save"', source)
        self.assertIn('"export_brain_learnings_context": _export_brain_learnings_context', source)
        self.assertIn("async def _export_brain_learnings_context(args: dict)", source)
        self.assertIn('payload = await _get_brain_learnings(args)', source)
        self.assertIn('parsed.get("agent_context")', source)
        self.assertIn('"decision_queue": _build_brain_learning_decision_queue(', source)
        self.assertIn('"suggested_strategy_views": _build_brain_learning_strategy_views(', source)
        self.assertIn('"suggested_timeseries_views": _build_brain_learning_timeseries_views(', source)
        self.assertIn('"timeseries_query": _brain_learning_timeseries_query(', source)
        self.assertIn(
            '"export_performance_timeseries_context": _export_performance_timeseries_context',
            source,
        )
        self.assertIn("async def _export_performance_timeseries_context(args: dict)", source)
        self.assertIn('payload = await _get_performance_timeseries(args)', source)
        self.assertIn("Performance timeseries response did not include agent_context", source)
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

    def test_demographic_focus_views_include_segment_specific_mixed_queries(self) -> None:
        namespace = _load_pure_helpers(
            {
                "_demographic_segment_label",
                "_compact_demographic_segment",
                "_format_demographic_evidence",
                "_build_demographics_strategy_query",
                "_build_demographic_focus_views",
                "_build_demographic_timeseries_query",
                "_build_demographic_segment_timeseries_views",
            }
        )

        focus_views = namespace["_build_demographic_focus_views"](
            [
                {
                    "age": "25-34",
                    "gender": "female",
                    "observed_efficiency_band": "higher_observed_return_per_spend",
                    "return_per_spend_percentile": 100,
                    "spend": 420,
                    "revenue": 1680,
                    "roas": 4.0,
                    "ctr": 2.6,
                    "cpa": 35,
                    "conversions": 12,
                }
            ],
            brand_name="Acme",
            date_preset="last_30_days",
            limit=1,
        )

        self.assertEqual(len(focus_views), 1)
        first = focus_views[0]
        self.assertEqual(first["segment"], "25-34 / female")
        self.assertEqual(
            first["observed_efficiency_band"],
            "higher_observed_return_per_spend",
        )
        self.assertIn("$420 spend", first["evidence_summary"])
        self.assertEqual(len(first["strategy_views"]), 2)
        labels = {view["label"] for view in first["strategy_views"]}
        self.assertEqual(labels, {"Angles for 25-34 / female", "Hooks for 25-34 / female"})
        for view in first["strategy_views"]:
            query = view["strategy_query"]
            self.assertEqual(query["tool"], "get_creative_strategy_report")
            self.assertEqual(query["brand_name"], "Acme")
            self.assertEqual(query["date_preset"], "last_30_days")
            self.assertEqual(query["focus_segment"], "25-34 / female")
            self.assertEqual(query["columns"], "demographic_segment")

        timeseries_views = namespace["_build_demographic_segment_timeseries_views"](
            [
                {
                    "age": "25-34",
                    "gender": "female",
                    "observed_efficiency_band": "higher_observed_return_per_spend",
                    "return_per_spend_percentile": 100,
                    "spend": 420,
                    "revenue": 1680,
                    "roas": 4.0,
                    "ctr": 2.6,
                    "cpa": 35,
                    "conversions": 12,
                }
            ],
            brand_name="Acme",
            date_preset="last_30_days",
            limit=1,
        )

        self.assertEqual(len(timeseries_views), 1)
        timeseries_item = timeseries_views[0]
        self.assertEqual(timeseries_item["segment"], "25-34 / female")
        self.assertEqual(len(timeseries_item["timeseries_views"]), 1)
        trend = timeseries_item["timeseries_views"][0]
        self.assertEqual(trend["label"], "Trend for 25-34 / female")
        self.assertEqual(trend["timeseries_query"]["tool"], "get_performance_timeseries")
        self.assertEqual(trend["timeseries_query"]["brand_name"], "Acme")
        self.assertEqual(trend["timeseries_query"]["group_by"], "demographic_segment")
        self.assertEqual(trend["timeseries_query"]["focus_value"], "25-34 / female")
        self.assertEqual(trend["timeseries_query"]["date_preset"], "last_30_days")

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
    dependencies = {"_clamped_int_arg"} if "_strategy_params" in wanted else set()
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted | dependencies
    ]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace = {
        "json": json,
        "STRATEGY_DECISION_LIMIT": 25,
        "STRATEGY_WATCH_LIMIT": 10,
        "_text": lambda payload: [SimpleNamespace(text=json.dumps(payload, indent=2))],
    }
    exec(compile(module, str(SERVER), "exec"), namespace)
    return namespace


if __name__ == "__main__":
    unittest.main()
