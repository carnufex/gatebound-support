"""Entry points for ``gatebound-support voicebot``: the real run loop, and ``--check``
(validates settings, loads opus, imports everything, exits without connecting)."""

from __future__ import annotations

import sys

from ..logging import configure_logging, get_logger
from ..settings import Settings, enabled

logger = get_logger("gatebound_support.voicebot.runner")

_REQUIRED_FOR_VOICE = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_SUPPORT_CHANNEL_ID",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_AGENT_ID",
    "MCP_SECRET",
)


def check(settings: Settings) -> int:
    """Validates settings, loads the Opus codec, and imports the whole voicebot package
    without connecting to Discord or ElevenLabs. Exits non-zero (returns 1) if anything
    required is missing or import fails, so this can be used as a container startup /
    CI smoke check."""
    problems: list[str] = []

    for name in _REQUIRED_FOR_VOICE:
        if not enabled(getattr(settings, name)):
            problems.append(f"{name} is unset")

    try:
        int(settings.DISCORD_GUILD_ID)
    except ValueError:
        problems.append("DISCORD_GUILD_ID is not a valid Discord snowflake")

    try:
        int(settings.DISCORD_SUPPORT_CHANNEL_ID)
    except ValueError:
        problems.append("DISCORD_SUPPORT_CHANNEL_ID is not a valid Discord snowflake")

    if settings.VOICE_MAX_MINUTES <= 0:
        problems.append("VOICE_MAX_MINUTES must be positive")

    try:
        from .opus_support import ensure_opus_loaded

        if not ensure_opus_loaded():
            problems.append("libopus could not be loaded (install libopus0)")

        # Touch every submodule so a broken import surfaces here, not at 3am on restart.
        from . import (  # noqa: F401
            audio_source,
            bot,
            elevenlabs_bridge,
            opus_support,
            resampler,
            service_client,
            sink,
            state,
            tickets,
        )
    except Exception:  # pragma: no cover - exercised via the CLI, not pytest
        logger.exception("voicebot check: import failed")
        problems.append("import failed, see the logged traceback above")

    if problems:
        for problem in problems:
            print(f"voicebot check: {problem}", file=sys.stderr)
        return 1

    print("voicebot check: ok")
    return 0


def run(settings: Settings) -> int:
    configure_logging(settings.LOG_LEVEL)
    if not enabled(settings.DISCORD_BOT_TOKEN):
        logger.error("DISCORD_BOT_TOKEN is unset; the voicebot cannot start")
        return 1

    from .bot import VoiceBot
    from .opus_support import ensure_opus_loaded

    if not ensure_opus_loaded():
        logger.warning("libopus could not be loaded; voice will not work")

    bot = VoiceBot(settings)
    bot.run(settings.DISCORD_BOT_TOKEN, log_handler=None)
    return 0
