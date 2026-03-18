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
        "hook_type": {
            "description": "Creative hook in first 1-3 seconds",
            "values": [
                "UGC", "ProdShot", "Testi", "ProbAg", "BA", "Unbox", "Life",
                "TalkHead", "TextOvr", "Mashup", "Demo", "SocProof", "Founder",
                "Compare", "Tutorial", "Meme", "StopScrl", "Other",
            ],
        },
        "hook_style": {
            "description": "How the hook is executed",
            "values": [
                "TextOnly", "TalkingHead", "VOBroll", "NativeCaption", "SplitScreen",
                "GreenScreen", "ScreenRec", "ProdCloseup", "TrendAudio", "StopMotion",
                "StitchDuet", "PhotoMontage", "RapidCut", "ASMR", "HandDrawn",
            ],
        },
        "visual_style": {
            "description": "Overall visual aesthetic",
            "values": [
                "Min", "Bold", "Org", "Dark", "Bright", "Edit", "LoFi", "HiFi",
                "Native", "Brand", "Retro", "Pastel", "HiContrast", "Editorial",
            ],
        },
        "talent_type": {
            "description": "Who appears in the creative",
            "values": [
                "NoTalent", "Creator", "Model", "Founder", "Customer", "VO", "Hands",
                "Influencer", "Expert", "Employee", "AIAvatar", "Celebrity",
                "MultiCreator", "Family", "Animated",
            ],
        },
        "cta_type": {
            "description": "Call-to-action",
            "values": [
                "ShopNow", "LearnMore", "SignUp", "GetOffer", "BookNow", "Download",
                "Subscribe", "WatchMore", "SwipeUp", "GetStarted", "TryFree",
                "GetQuote", "AddToCart", "TakeQuiz", "ClaimDisc", "SeePlans",
                "SendMsg", "ApplyNow", "None", "Other",
            ],
        },
        "cta_placement": {
            "description": "Where/how the CTA appears",
            "values": [
                "EndCard", "Verbal", "Persistent", "CaptionOnly", "MidRoll",
                "ButtonOvr", "SwipeLink", "Multiple", "NoCTA",
            ],
        },
        "primary_emotion": {
            "description": "Dominant emotional tone",
            "values": [
                "Urgent", "Curious", "Trust", "Fear", "Desire", "Humor", "Aspire",
                "Relief", "Neutral", "Emotional", "Empower", "Calming", "Nostalgic",
                "Shocking", "Playful",
            ],
        },
        "messaging_angle": {
            "description": "Persuasion architecture / narrative framework",
            "values": [
                "ProbSol", "BeforeAfter", "SocProof", "FearRisk", "Aspire", "Edu",
                "Compare", "Scarcity", "ValuePrice", "Convenience", "EmoStory",
                "MythBust", "BTS", "Community", "Guilt", "Novelty", "TrendRide",
                "Health", "ProDev", "Guarantee",
            ],
        },
        "creative_type": {
            "description": "Creative concept archetype",
            "values": [
                "Testimonial", "ProdDemo", "FounderStory", "Lifestyle", "Unboxing",
                "Explainer", "Transform", "Comparison", "Listicle", "Skit", "PodClip",
                "GRWM", "DayInLife", "Reaction", "Meme", "Advertorial", "Mashup",
                "ProbAgit", "ProdOnly", "Catalog", "ScreenTut", "Announce",
                "ManOnStreet", "SplitScreen",
            ],
        },
        "production_type": {
            "description": "Production quality / style",
            "values": [
                "HighProd", "LoFiUGC", "AuthUGC", "LifeLoc", "ScreenRec", "MoGraph",
                "AIGen", "MixedMedia", "StockOvr", "PodVisual", "StopMotion",
                "Whiteboard",
            ],
        },
        "product_presence": {
            "description": "How the product appears",
            "values": [
                "Hero", "Integrated", "Secondary", "Reveal", "EndCard", "NotShown",
                "Multiple", "Packaging", "Ingredient", "Closeup",
            ],
        },
        "offer_type": {
            "description": "Promotional structure",
            "values": [
                "PctOff", "DollarOff", "FreeShip", "FreeGift", "BOGO", "Bundle",
                "FreeTrial", "SubDisc", "MoneyBack", "LimitedTime", "FlashSale",
                "Exclusive", "Loyalty", "PriceAnchor", "NoOffer", "Charitable", "BNPL",
            ],
        },
        "social_proof_elements": {
            "description": "Credibility signals present (can be multiple)",
            "values": [
                "StarRating", "ReviewQuote", "ReviewCount", "CustCount", "PressLogos",
                "ExpertEnd", "CelebEnd", "BAPhotos", "Awards", "RealTime",
                "UGCCompile", "UserPhotos", "CaseStudy", "ClinicalData", "CommSize",
                "NoProof",
            ],
        },
        "brand_presence": {
            "description": "How the brand appears",
            "values": [
                "LogoAll", "LogoEnd", "LogoProd", "NoLogo", "BrandColors",
                "BrandIntro", "BrandText", "BrandSpoken", "Unbranded",
            ],
        },
        "seasonality": {
            "description": "Temporal context",
            "values": [
                "Evergreen", "BFCM", "Holiday", "NewYear", "Valentines", "MothFath",
                "BTS", "Summer", "Spring", "Fall", "PrimeDay", "TaxSeason", "Wedding",
                "Sports", "Cultural",
            ],
        },
        "text_overlay_treatment": {
            "description": "On-screen text style",
            "values": [
                "NoText", "Minimal", "Moderate", "Heavy", "Subtitles", "Kinetic",
                "LowerThird", "FullCards", "Handwritten", "BrandStyled",
            ],
        },
        "audio_type": {
            "description": "Primary audio composition",
            "values": [
                "voiceover_only", "music_only", "vo_over_music", "dialogue",
                "sfx_only", "silent", "mixed",
            ],
        },
        "video_length_bucket": {
            "description": "Video duration classification",
            "values": [
                "Micro (0-6s)", "Short (6-15s)", "Standard (15-30s)",
                "Medium (30-60s)", "Long (60-90s)", "Extended (90-180s)",
                "LongForm (180s+)",
            ],
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
