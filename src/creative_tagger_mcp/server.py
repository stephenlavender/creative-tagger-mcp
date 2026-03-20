"""Creative Tagger MCP Server.

Exposes creative analysis tools via the Model Context Protocol,
enabling any AI agent to classify ad creatives against a standardized
28-dimension taxonomy.

Usage:
    creative-tagger-mcp                          # defaults to localhost:8000
    CREATIVE_TAGGER_URL=https://api.creativetagger.dev creative-tagger-mcp
"""

import base64
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_URL = os.environ.get("CREATIVE_TAGGER_URL", "http://localhost:8000")
API_KEY = os.environ.get("CREATIVE_TAGGER_API_KEY", "")

server = Server("creative-tagger")


def _headers() -> dict:
    h = {}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


# ---------- Tools ----------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="analyze_creative",
            description=(
                "Analyze any ad creative (image, video, landing page, email) "
                "and return structured classification across 28 taxonomy dimensions: "
                "hook type, hook style, messaging angle, creative type, visual style, "
                "talent type, CTA, emotion, production type, offer type, social proof, "
                "product presence, brand presence, seasonality, and more. "
                "Also generates standardized naming conventions."
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
                            "URL to analyze. Can be a direct file URL (image/video) "
                            "or a landing page URL for screenshot analysis."
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
                "Get the complete Creative Tagger taxonomy — all 28 dimensions "
                "with every enum value. Use this to understand what classifications "
                "are available before analyzing a creative."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": (
                            "Optional: get values for a specific dimension only. "
                            "e.g. 'hook_type', 'messaging_angle', 'creative_type'"
                        ),
                    },
                },
            },
        ),
        Tool(
            name="generate_naming",
            description=(
                "Generate standardized naming convention strings from creative "
                "attributes. Input the classified attributes and get back naming "
                "strings in multiple formats (default, compact, extended)."
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
    if name == "analyze_creative":
        return await _analyze_creative(arguments)
    elif name == "get_taxonomy":
        return _get_taxonomy(arguments)
    elif name == "generate_naming":
        return _generate_naming(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------- Tool Implementations ----------


async def _analyze_creative(args: dict) -> list[TextContent]:
    """Send creative to the API for analysis."""
    file_path = args.get("file_path")
    url = args.get("url")
    html_content = args.get("html_content")
    brand_name = args.get("brand_name", "Brand")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if file_path:
                path = Path(file_path).expanduser().resolve()
                if not path.exists():
                    return [TextContent(type="text", text=f"File not found: {file_path}")]

                with open(path, "rb") as f:
                    resp = await client.post(
                        f"{API_URL}/analyze",
                        files={"file": (path.name, f)},
                        data={"brand_name": brand_name},
                        headers=_headers(),
                    )

            elif url:
                # Detect if it's a landing page or direct file URL
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
                    f"{API_URL}/analyze",
                    data=data,
                    headers=_headers(),
                )

            elif html_content:
                resp = await client.post(
                    f"{API_URL}/analyze",
                    data={"brand_name": brand_name, "html_content": html_content},
                    headers=_headers(),
                )
            else:
                return [TextContent(type="text", text="Provide file_path, url, or html_content")]

            resp.raise_for_status()
            result = resp.json()
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except httpx.HTTPStatusError as e:
        body = e.response.json() if e.response else {}
        msg = body.get("detail", str(e))
        return [TextContent(type="text", text=f"API error: {msg}")]
    except httpx.ConnectError:
        return [
            TextContent(
                type="text",
                text=(
                    f"Cannot connect to Creative Tagger API at {API_URL}. "
                    "Make sure the server is running or set CREATIVE_TAGGER_URL."
                ),
            )
        ]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


def _get_taxonomy(args: dict) -> list[TextContent]:
    """Return the full taxonomy or a specific dimension."""
    dimension = args.get("dimension")

    taxonomy = {
        "asset_type": {
            "description": "Broad creative format and production level",
            "values": [
                "UGC", "Lifestyle", "Product Shot", "Studio", "High Production",
                "Screen Recording", "Stock", "AI Generated", "Animation", "Mixed Media",
            ],
        },
        "visual_format": {
            "description": "Specific execution style",
            "values": [
                "Talking Head", "Testimonial", "Before After", "Unboxing",
                "Problem Agitate", "Listicle", "Text Overlay", "Mashup", "Demo",
                "Social Proof", "Founder Story", "Comparison", "Tutorial", "Meme",
                "Scroll Stopper", "Skit", "Podcast Clip", "Green Screen",
                "Slideshow", "Carousel", "Static Image",
            ],
        },
        "visual_style": {
            "description": "Look and feel / aesthetic",
            "values": [
                "Minimal", "Bold", "Organic", "Dark", "Bright", "Editorial",
                "Lo-Fi", "Hi-Fi", "Native Feel", "Branded", "Retro", "Clean",
            ],
        },
        "talent": {
            "description": "Who is featured",
            "values": [
                "No Talent", "Creator", "Model", "Founder", "Customer",
                "Voiceover Only", "Hands Only", "Employee", "Expert", "Influencer",
            ],
        },
        "audience": {
            "description": "Target persona (AI-generated per brand)",
            "values": "dynamic",
            "examples": [
                "New Moms", "Wellness Seekers", "Budget Conscious",
                "Fitness Enthusiasts", "Gift Shoppers",
            ],
        },
        "messaging_angle": {
            "description": "Narrative/persuasion approach (AI-generated per brand)",
            "values": "dynamic",
            "examples": [
                "Pain Point", "Aspiration", "Social Proof", "Education",
                "Scarcity", "Value Prop", "Lifestyle", "Authority",
            ],
        },
        "hook_type": {
            "description": "What stops the scroll in first 1-3 seconds",
            "values": [
                "Question", "Bold Claim", "Callout", "Contrarian", "Confession",
                "If Then", "Statistic", "Urgency", "Curiosity Gap", "Social Proof",
                "Pain Point", "Transformation", "Challenge", "Story Open",
                "Pattern Interrupt",
            ],
        },
        "cta": {
            "description": "Call to action",
            "values": [
                "Shop Now", "Learn More", "Sign Up", "Get Offer", "Book Now",
                "Download", "Subscribe", "Watch More", "Swipe Up", "Try Free",
                "No CTA",
            ],
        },
        "audio_type": {
            "description": "Primary audio treatment",
            "values": [
                "Voiceover + Music", "Voiceover Only", "Music Only",
                "Trending Sound", "Native Audio", "Silent",
            ],
        },
        "voiceover_tone": {
            "description": "Spoken audio delivery style",
            "values": [
                "Conversational", "Urgent", "Authoritative", "Friendly",
                "Whispery", "Energetic", "Calm", "None",
            ],
        },
        "emotion": {
            "description": "Primary emotional trigger",
            "values": [
                "Urgency", "Curiosity", "Trust", "Fear", "Desire",
                "Humor", "Aspiration", "Relief", "Belonging", "Neutral",
            ],
        },
        "seasonality": {
            "description": "Temporal context",
            "values": [
                "Evergreen", "Black Friday", "Cyber Monday", "Holiday",
                "New Year", "Valentines", "Mothers Day", "Fathers Day",
                "Back To School", "Summer", "Spring", "Fall",
                "Prime Day", "Launch", "Flash Sale",
            ],
        },
        "offer_type": {
            "description": "Promotional lever",
            "values": [
                "No Offer", "Percent Off", "Dollar Off", "Free Shipping",
                "BOGO", "Bundle", "Free Gift", "Subscribe Save",
                "Limited Time", "Clearance",
            ],
        },
        "duration": {
            "description": "Video duration bucket",
            "values": ["6s", "15s", "30s", "60s", "90s+"],
        },
    }

    if dimension:
        dim = taxonomy.get(dimension)
        if dim:
            return [TextContent(type="text", text=json.dumps({dimension: dim}, indent=2))]
        return [TextContent(type="text", text=f"Unknown dimension: {dimension}. Available: {', '.join(taxonomy.keys())}")]

    return [TextContent(type="text", text=json.dumps(taxonomy, indent=2))]


def _generate_naming(args: dict) -> list[TextContent]:
    """Generate naming convention strings from attributes."""
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

    result = {
        "default": default,
        "compact": compact,
        "note": "For full naming with all 28 dimensions, use analyze_creative",
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


# ---------- Main ----------


def main():
    """Run the MCP server."""
    import asyncio

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
