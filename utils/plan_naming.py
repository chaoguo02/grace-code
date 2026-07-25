"""Plan file naming — CC-aligned human-readable word slugs.

Generates two-word random slugs (e.g. "bold-eagle", "calm-river")
instead of exposing internal session IDs in plan filenames.

Slugs are unique per-repo; collision retries up to 10 times.
"""

from __future__ import annotations

import random

_ADJECTIVES = [
    "bold", "calm", "eager", "kind", "warm", "wise", "cool", "keen",
    "bright", "sharp", "swift", "brave", "clear", "deep", "fair", "grand",
]

_NOUNS = [
    "eagle", "hawk", "wolf", "bear", "deer", "dove", "lynx", "owl",
    "fox", "wren", "swan", "crow", "frog", "newt", "trout", "crane",
    "maple", "oak", "pine", "elm", "ash", "birch", "cedar", "fir",
    "river", "stone", "cloud", "star", "moon", "dawn", "mist", "peak",
]


def generate_plan_slug(existing_slugs: set[str] | None = None) -> str:
    """Generate a unique human-readable plan file name.

    Returns a string like ``"bold-eagle"``.  Retries up to 10 times
    if the slug collides with an existing filename.
    Falls back to appending a random suffix on exhaustion.
    """
    existing = existing_slugs or set()
    rng = random.SystemRandom()
    for _ in range(10):
        adj = rng.choice(_ADJECTIVES)
        noun = rng.choice(_NOUNS)
        slug = f"{adj}-{noun}"
        if slug not in existing:
            return slug
    # Exhausted retries — append 4 random hex chars
    adj = rng.choice(_ADJECTIVES)
    noun = rng.choice(_NOUNS)
    suffix = f"{random.randint(0, 0xFFFF):04x}"
    return f"{adj}-{noun}-{suffix}"
