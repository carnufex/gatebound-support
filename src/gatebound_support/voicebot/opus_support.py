"""Makes sure libopus is loaded before any voice playback/encoding happens.

discord.py only loads Opus lazily, the first time something like ``Encoder()`` is
constructed (``.venv/Lib/site-packages/discord/opus.py``: ``Encoder.get_opus_version`` -- a
side-effect-free static method -- does exactly ``if not is_loaded() and not _load_default():
raise OpusNotLoaded()``, which is the public-ish trigger this module reuses instead of
duplicating that private-function logic). ``_load_default()`` uses
``ctypes.util.find_library('opus')`` on Linux/macOS and the packaged DLL on Windows.

In a minimal Debian container, ``ctypes.util.find_library('opus')`` can come up empty even
though ``libopus0`` is installed (a known discord.py-in-Docker gotcha, which is why the
Dockerfile installs it and this module has an explicit fallback name to try).
"""

from __future__ import annotations

import discord

from ..logging import get_logger

logger = get_logger("gatebound_support.voicebot.opus")

_EXPLICIT_FALLBACK_NAME = "libopus.so.0"


def ensure_opus_loaded() -> bool:
    """Returns True if Opus ends up loaded (already was, or one of the two attempts here
    loaded it), False otherwise. Never raises."""
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus.Encoder.get_opus_version()
    except discord.opus.OpusNotLoaded:
        pass
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus.load_opus(_EXPLICIT_FALLBACK_NAME)
    except OSError:
        logger.warning("libopus could not be loaded", extra={"fields": {"tried": _EXPLICIT_FALLBACK_NAME}})
        return False
    return discord.opus.is_loaded()
