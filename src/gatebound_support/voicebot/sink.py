"""The voice-recv ``AudioSink`` that captures one Discord user's decoded PCM and hands it,
resampled, to the ElevenLabs bridge.

Verified against the installed ``discord-ext-voice-recv==0.5.2a179`` package
(``.venv/Lib/site-packages/discord/ext/voice_recv/sinks.py``): ``AudioSink`` is an ABC
requiring ``wants_opus() -> bool``, ``write(user, data: VoiceData)`` and ``cleanup()``.
``VoiceData.pcm`` (``opus.py``) is already-decoded PCM once ``wants_opus()`` returns
``False`` — no manual Opus decoding needed here. The PCM format matches
``discord.player.AudioSource``'s own contract (16-bit 48 kHz stereo), confirmed by
``discord/opus.py``'s ``Decoder``/``Encoder`` constants (``SAMPLING_RATE = 48000``,
``CHANNELS = 2``).
"""

from __future__ import annotations

from collections.abc import Callable

from discord.ext import voice_recv

from ..logging import get_logger
from .resampler import discord_frame_to_agent_pcm

logger = get_logger("gatebound_support.voicebot.sink")


class SinglePlayerSink(voice_recv.AudioSink):
    """Only the command invoker's audio is bridged to the agent (SPEC: "Handle only ONE
    speaking user's stream at a time"); everyone else in the channel is silently dropped
    rather than mixed in, so the agent doesn't hear cross-talk from spectators."""

    def __init__(self, *, target_user_id: int, on_player_audio: Callable[[bytes], None]) -> None:
        super().__init__()
        self._target_user_id = target_user_id
        self._on_player_audio = on_player_audio

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None or user.id != self._target_user_id:
            return
        if not data.pcm:
            return
        try:
            self._on_player_audio(discord_frame_to_agent_pcm(data.pcm))
        except Exception:
            logger.exception("player audio handling failed")

    def cleanup(self) -> None:
        pass
