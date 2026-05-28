"""Smoke tests for the documented MCP V1 tool surface.

These parse the source instead of importing it, so they can run in a clean
workspace before the optional MCP runtime dependency is installed.
"""

from __future__ import annotations

import ast
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
    "set_brand_entity",
    "get_naming_variables",
    "list_naming_templates",
    "save_naming_template",
    "delete_naming_template",
    "preview_naming_template",
    "get_meta_status",
    "sync_meta_performance",
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


def _declared_tool_names() -> set[str]:
    tree = ast.parse(SERVER.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "id", "")
        if func_name != "Tool":
            continue
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                names.add(str(kw.value.value))
    return names


if __name__ == "__main__":
    unittest.main()
