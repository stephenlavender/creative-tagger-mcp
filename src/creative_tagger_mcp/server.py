"""Creative Tagger MCP Server.

Exposes the Creative Tagger API as MCP tools so any AI agent (Claude Desktop,
Cursor, Windsurf, ChatGPT with MCP, etc.) can:

- Analyze ad creatives across 28 taxonomy dimensions
- Browse and search the user's creative library (memory)
- Get strategist recommendations grounded in library + brand context
- Set brand voice / audience / top performers / anti-patterns
- Scan competitor ads from the Meta Ad Library

Usage:
    creative-tagger-mcp
    CREATIVE_TAGGER_URL=https://api.creativetagger.ai \\
    CREATIVE_TAGGER_API_KEY=ct_xxx creative-tagger-mcp
"""

import json
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_URL = os.environ.get("CREATIVE_TAGGER_URL", "https://api.creativetagger.ai")
API_KEY = os.environ.get("CREATIVE_TAGGER_API_KEY", "")

server = Server("creative-tagger")


def _headers() -> dict:
    h = {}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def _auth_params() -> dict:
    """Some endpoints take api_key as a query param rather than header."""
    return {"api_key": API_KEY} if API_KEY else {}


def _text(payload: Any) -> list[TextContent]:
    """Wrap any JSON-able payload as a TextContent response."""
    if isinstance(payload, str):
        return [TextContent(type="text", text=payload)]
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


# ---------- Tools ----------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="analyze_creative",
            description=(
                "Analyze any ad creative (image, video, carousel, landing page, email) "
                "and return structured classification across 28 taxonomy dimensions: "
                "hook type, messaging angle, creative type, visual style, talent, CTA, "
                "emotion, production type, offer type, social proof, brand presence, "
                "seasonality, audio attributes, and more. Also generates standardized "
                "naming conventions. Provide one of: file_path, url, or html_content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Local file path to analyze (image or video)",
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "URL to analyze. Direct file URL (image/video) or landing page URL."
                        ),
                    },
                    "html_content": {
                        "type": "string",
                        "description": "Raw HTML for email creative analysis",
                    },
                    "brand_name": {
                        "type": "string",
                        "description": "Brand name for naming conventions",
                        "default": "Brand",
                    },
                },
                "oneOf": [
                    {"required": ["file_path"]},
                    {"required": ["url"]},
                    {"required": ["html_content"]},
                ],
            },
        ),
        Tool(
            name="get_taxonomy",
            description=(
                "Get the complete Creative Tagger taxonomy — all 28 dimensions with "
                "every enum value. Pulled live from the API so it's always current. "
                "Use this before analyze_creative when you want to know the full "
                "vocabulary the system understands."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": (
                            "Optional: fetch one dimension only "
                            "(e.g. 'hook_type', 'messaging_angle')."
                        ),
                    },
                },
            },
        ),
        Tool(
            name="list_library",
            description=(
                "Browse the authenticated user's saved analysis library (memory). "
                "Every analyze_creative call is automatically saved. Use this to "
                "recall what has been analyzed before — search by filename, hook, "
                "or filter by format/hook type. Returns items in reverse-chronological "
                "order."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                    "search": {
                        "type": "string",
                        "description": "Search filename, naming, hook, or creative type",
                    },
                    "format": {
                        "type": "string",
                        "description": "Filter by format: video, image, carousel, landing_page, email, long_video",
                    },
                    "hook": {
                        "type": "string",
                        "description": "Filter by hook type (UGC, Demo, TalkHead, etc.)",
                    },
                },
            },
        ),
        Tool(
            name="get_library_patterns",
            description=(
                "Get pattern insights across the user's entire library: which hooks, "
                "angles, creative types, emotions they over- or under-index on. "
                "Returns top values per dimension with percentages plus "
                "rule-based diversification insights. Use this for portfolio analysis "
                "before recommending what to make next."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_analysis",
            description=(
                "Get the full analysis result for a single saved library item by ID. "
                "Use after list_library when you need the complete 28-dimension classification "
                "(list_library returns a summary; this returns the full JSON)."
            ),
            inputSchema={
                "type": "object",
                "required": ["analysis_id"],
                "properties": {
                    "analysis_id": {
                        "type": "integer",
                        "description": "ID of the analysis to fetch",
                    },
                },
            },
        ),
        Tool(
            name="recommend",
            description=(
                "Ask the Creative Strategist a question, grounded in the user's library + "
                "brand context. The strategist auto-loads patterns from prior analyses "
                "and any saved brand voice/audience/anti-patterns for the brand, then "
                "answers with concrete creative recommendations using taxonomy values. "
                "Use this for open-ended strategic questions ('what should I test next', "
                "'how should I approach Q4', 'what kind of UGC would work for this audience')."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "question"],
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": "Brand to ground the recommendation in",
                    },
                    "question": {
                        "type": "string",
                        "description": "The strategic question",
                    },
                },
            },
        ),
        Tool(
            name="analyze_gaps",
            description=(
                "Identify gaps in the user's creative library for a given brand and "
                "propose concrete next creatives that fill them. Surfaces concentration "
                "risk (e.g., 78% UGC TalkHead) and recommends under-represented hook "
                "types, messaging angles, creative types. Returns JSON with gap analysis + "
                "ready-to-produce briefs."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {
                        "type": "string",
                        "description": "Brand to analyze",
                    },
                },
            },
        ),
        Tool(
            name="get_brand_context",
            description=(
                "Retrieve the saved brand context for a brand: voice, target audience, "
                "top performers, anti-patterns, and notes. Strategist tools auto-include "
                "this; this tool exposes the raw stored context."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                },
            },
        ),
        Tool(
            name="set_brand_context",
            description=(
                "Create or update brand context for a brand. Stored per-user. This is "
                "the brand's long-term memory — voice, audience, what works, what to "
                "avoid. Future strategist and brief calls automatically include this "
                "context. Upserts on (user, brand_name)."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "voice": {
                        "type": "string",
                        "description": "Brand voice / tone (e.g., 'clinical, precise')",
                    },
                    "target_audience": {
                        "type": "string",
                        "description": "Who the brand is for",
                    },
                    "top_performers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Patterns/elements that work for this brand",
                    },
                    "anti_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Patterns/elements to avoid for this brand",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Free-form additional context",
                    },
                },
            },
        ),
        Tool(
            name="scan_competitor",
            description=(
                "Scan a competitor's ads from the Meta Ad Library and return classified "
                "results plus an aggregate strategy breakdown (top hook types, visual "
                "styles, CTAs, emotions, estimated spend). Provide page_id, page_name, "
                "or keyword. Returns ad metadata, full Creative Tagger analysis per ad, "
                "and strategy insights."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string"},
                    "page_name": {"type": "string"},
                    "keyword": {"type": "string"},
                    "country": {"type": "string", "default": "US"},
                    "limit": {"type": "integer", "default": 25},
                    "analyze_creatives": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="generate_naming",
            description=(
                "Generate standardized naming convention strings from creative attributes. "
                "Use when you already have classified attributes (e.g., from a prior "
                "analyze_creative call) and just need the naming output."
            ),
            inputSchema={
                "type": "object",
                "required": ["brand_name", "hook_type", "cta_type", "aspect_ratio"],
                "properties": {
                    "brand_name": {"type": "string"},
                    "hook_type": {"type": "string"},
                    "visual_style": {"type": "string", "default": "Native"},
                    "talent_type": {"type": "string", "default": "NoTalent"},
                    "cta_type": {"type": "string"},
                    "aspect_ratio": {"type": "string", "default": "9:16"},
                    "audio_shortcode": {"type": "string", "default": "Silent"},
                    "version": {"type": "integer", "default": 1},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "analyze_creative":
            return await _analyze_creative(arguments)
        if name == "get_taxonomy":
            return await _get_taxonomy(arguments)
        if name == "list_library":
            return await _list_library(arguments)
        if name == "get_library_patterns":
            return await _get_library_patterns(arguments)
        if name == "get_analysis":
            return await _get_analysis(arguments)
        if name == "recommend":
            return await _recommend(arguments)
        if name == "analyze_gaps":
            return await _analyze_gaps(arguments)
        if name == "get_brand_context":
            return await _get_brand_context(arguments)
        if name == "set_brand_context":
            return await _set_brand_context(arguments)
        if name == "scan_competitor":
            return await _scan_competitor(arguments)
        if name == "generate_naming":
            return _generate_naming(arguments)
        return _err(f"Unknown tool: {name}")
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        return _err(f"API error ({e.response.status_code}): {detail}")
    except httpx.ConnectError:
        return _err(
            f"Cannot connect to Creative Tagger API at {API_URL}. "
            "Set CREATIVE_TAGGER_URL or check the API is running."
        )
    except Exception as e:
        return _err(str(e))


# ---------- Tool Implementations ----------


async def _analyze_creative(args: dict) -> list[TextContent]:
    file_path = args.get("file_path")
    url = args.get("url")
    html_content = args.get("html_content")
    brand_name = args.get("brand_name", "Brand")

    async with httpx.AsyncClient(timeout=180.0) as client:
        if file_path:
            path = Path(file_path).expanduser().resolve()
            if not path.exists():
                return _err(f"File not found: {file_path}")
            with open(path, "rb") as f:
                resp = await client.post(
                    f"{API_URL}/analyze",
                    files={"file": (path.name, f)},
                    data={"brand_name": brand_name},
                    headers=_headers(),
                )
        elif url:
            is_page = not any(
                url.lower().endswith(ext)
                for ext in (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp", ".gif")
            )
            data = {"brand_name": brand_name}
            if is_page:
                data["page_url"] = url
            else:
                data["file_url"] = url
            resp = await client.post(
                f"{API_URL}/analyze", data=data, headers=_headers()
            )
        elif html_content:
            resp = await client.post(
                f"{API_URL}/analyze",
                data={"brand_name": brand_name, "html_content": html_content},
                headers=_headers(),
            )
        else:
            return _err("Provide file_path, url, or html_content")

        resp.raise_for_status()
        return _text(resp.json())


async def _get_taxonomy(args: dict) -> list[TextContent]:
    dimension = args.get("dimension")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/openapi.json", headers=_headers())
        resp.raise_for_status()
        spec = resp.json()

    schemas = (spec.get("components") or {}).get("schemas") or {}
    enums: dict[str, list[str]] = {}
    for name, schema in schemas.items():
        values = schema.get("enum")
        if values and isinstance(values, list):
            enums[name] = values

    if dimension:
        match = next(
            (v for k, v in enums.items() if k.lower() == dimension.lower()),
            None,
        )
        if not match:
            return _err(
                f"Unknown dimension: {dimension}. Available: {', '.join(sorted(enums.keys()))}"
            )
        return _text({dimension: match})

    return _text({"dimensions": enums, "count": len(enums)})


async def _list_library(args: dict) -> list[TextContent]:
    params: dict[str, Any] = {**_auth_params()}
    for k in ("limit", "offset", "search", "format", "hook"):
        if args.get(k) is not None:
            params[k] = args[k]
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/auth/library", params=params)
        resp.raise_for_status()
        return _text(resp.json())


async def _get_library_patterns(args: dict) -> list[TextContent]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/auth/library/patterns", params=_auth_params()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_analysis(args: dict) -> list[TextContent]:
    analysis_id = args.get("analysis_id")
    if not analysis_id:
        return _err("analysis_id is required")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{API_URL}/auth/library/{analysis_id}", params=_auth_params()
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _recommend(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    question = args.get("question", "")
    if not brand_name or not question:
        return _err("brand_name and question are required")
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_URL}/strategist/recommend",
            data={"brand_name": brand_name, "question": question},
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _analyze_gaps(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{API_URL}/strategist/gaps",
            data={"brand_name": brand_name},
            headers=_headers(),
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _get_brand_context(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    params = {**_auth_params(), "brand_name": brand_name}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API_URL}/auth/brand-context", params=params)
        if resp.status_code == 404:
            return _text(
                {"brand_name": brand_name, "exists": False, "message": "No brand context saved yet"}
            )
        resp.raise_for_status()
        return _text(resp.json())


async def _set_brand_context(args: dict) -> list[TextContent]:
    brand_name = args.get("brand_name", "")
    if not brand_name:
        return _err("brand_name is required")
    body = {
        "brand_name": brand_name,
        "voice": args.get("voice", ""),
        "target_audience": args.get("target_audience", ""),
        "top_performers": args.get("top_performers") or [],
        "anti_patterns": args.get("anti_patterns") or [],
        "notes": args.get("notes", ""),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{API_URL}/auth/brand-context",
            params=_auth_params(),
            json=body,
        )
        resp.raise_for_status()
        return _text(resp.json())


async def _scan_competitor(args: dict) -> list[TextContent]:
    body = {
        "page_id": args.get("page_id"),
        "page_name": args.get("page_name"),
        "keyword": args.get("keyword"),
        "country": args.get("country", "US"),
        "limit": args.get("limit", 25),
        "analyze_creatives": args.get("analyze_creatives", True),
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{API_URL}/competitors/scan", json=body, headers=_headers()
        )
        resp.raise_for_status()
        return _text(resp.json())


def _generate_naming(args: dict) -> list[TextContent]:
    brand = args.get("brand_name", "BRAND").upper()
    hook = args.get("hook_type", "Other")
    style = args.get("visual_style", "Native")
    talent = args.get("talent_type", "NoTalent")
    cta = args.get("cta_type", "None")
    ratio = args.get("aspect_ratio", "9:16").replace(":", "x")
    audio = args.get("audio_shortcode", "Silent")
    ver = f"V{args.get('version', 1)}"

    default = "_".join([brand, hook, talent, style, audio, cta, ratio, ver])
    compact = "_".join([brand, hook, cta, ratio, ver])
    return _text(
        {
            "default": default,
            "compact": compact,
            "note": "For naming with all 28 dimensions, use analyze_creative",
        }
    )


# ---------- Main ----------


def main():
    import asyncio

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
