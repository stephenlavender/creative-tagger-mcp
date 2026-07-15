"""Versioned Creative Tagger taxonomy vocabulary for the stdio MCP bridge.

The REST OpenAPI schema intentionally types several classification fields as
strings because brand-aware classifiers may preserve compatible custom values.
That makes OpenAPI enum discovery incomplete.  Keep the public MCP vocabulary
explicit and versioned instead of pretending the schema contains every value.
"""

from __future__ import annotations


TAXONOMY_VERSION = "v2"

# Controlled values verified against creative-tagger/app/taxonomy and
# CreativeFormat for taxonomy v2.  Tuples keep the package-level source
# immutable; callers receive fresh lists from ``taxonomy_payload`` below.
CONTROLLED_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "media_type": (
        "video",
        "image",
        "carousel",
        "landing_page",
        "email",
        "long_video",
    ),
    "asset_type": (
        "UGC",
        "Lifestyle",
        "Product Shot",
        "Studio",
        "High Production",
        "Screen Recording",
        "Stock",
        "AI Generated",
        "Animation",
        "Mixed Media",
    ),
    "visual_format": (
        "Talking Head",
        "Testimonial",
        "Before After",
        "Unboxing",
        "Problem Agitate",
        "Listicle",
        "Text Overlay",
        "Mashup",
        "Demo",
        "Social Proof",
        "Founder Story",
        "Comparison",
        "Tutorial",
        "Meme",
        "Scroll Stopper",
        "Skit",
        "Podcast Clip",
        "Green Screen",
        "Slideshow",
    ),
    "visual_style": (
        "Minimal",
        "Bold",
        "Organic",
        "Dark",
        "Bright",
        "Editorial",
        "Lo-Fi",
        "Hi-Fi",
        "Native Feel",
        "Branded",
        "Retro",
        "Clean",
    ),
    "talent": (
        "No Talent",
        "Creator",
        "Model",
        "Founder",
        "Customer",
        "Voiceover Only",
        "Hands Only",
        "Employee",
        "Expert",
        "Influencer",
    ),
    "talent_age_group": (
        "child",
        "teen",
        "age_18_24",
        "age_25_34",
        "age_35_44",
        "age_45_54",
        "age_55_plus",
        "mixed",
        "none",
    ),
    "talent_gender": ("female", "male", "mixed", "ambiguous", "none"),
    "hook_type": (
        "Question",
        "Bold Claim",
        "Callout",
        "Contrarian",
        "Confession",
        "If Then",
        "Statistic",
        "Urgency",
        "Curiosity Gap",
        "Social Proof",
        "Pain Point",
        "Transformation",
        "Challenge",
        "Story Open",
        "Pattern Interrupt",
    ),
    "cta": (
        "Shop Now",
        "Learn More",
        "Sign Up",
        "Get Offer",
        "Book Now",
        "Download",
        "Subscribe",
        "Watch More",
        "Swipe Up",
        "Try Free",
        "No CTA",
    ),
    "emotion": (
        "Urgency",
        "Curiosity",
        "Trust",
        "Fear",
        "Desire",
        "Humor",
        "Aspiration",
        "Relief",
        "Belonging",
        "Neutral",
    ),
    "audio_type": (
        "Voiceover + Music",
        "Voiceover Only",
        "Music Only",
        "Trending Sound",
        "Native Audio",
        "Silent",
    ),
    "voiceover_tone": (
        "Conversational",
        "Urgent",
        "Authoritative",
        "Friendly",
        "Whispery",
        "Energetic",
        "Calm",
        "None",
    ),
    "seasonality": (
        "Evergreen",
        "Black Friday",
        "Cyber Monday",
        "Holiday",
        "New Year",
        "Valentines",
        "Mothers Day",
        "Fathers Day",
        "Back To School",
        "Summer",
        "Spring",
        "Fall",
        "Prime Day",
        "Launch",
        "Flash Sale",
    ),
    "offer_type": (
        "No Offer",
        "Percent Off",
        "Dollar Off",
        "Free Shipping",
        "BOGO",
        "Bundle",
        "Free Gift",
        "Subscribe Save",
        "Limited Time",
        "Clearance",
    ),
    "aspect_ratio": ("1x1", "4x5", "5x4", "9x16", "16x9", "1.91x1"),
    "duration": ("6s", "15s", "30s", "60s", "90s+"),
}

DYNAMIC_DIMENSIONS: dict[str, str] = {
    "audience": (
        "Brand-specific intended audience label generated from the creative, "
        "such as New Moms or Wellness Seekers."
    ),
    "messaging_angle": (
        "Brand-specific 2-4 word persuasion label, such as Pain Point, "
        "Social Proof, or Aspiration."
    ),
}


def taxonomy_payload() -> dict[str, object]:
    """Return a JSON-safe copy of the complete controlled/dynamic vocabulary."""

    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "controlled_dimensions": {
            name: list(values) for name, values in CONTROLLED_DIMENSIONS.items()
        },
        "dynamic_dimensions": dict(DYNAMIC_DIMENSIONS),
        "controlled_dimension_count": len(CONTROLLED_DIMENSIONS),
        "dynamic_dimension_count": len(DYNAMIC_DIMENSIONS),
    }
