"""48 kHz stereo <-> 16 kHz mono PCM16 resampling, numpy-only (audioop is removed in Python
3.13, so this stays dependency-light and 3.11-compatible rather than reaching for it).

Discord gives/wants 16-bit 48 kHz stereo PCM (`discord.player.AudioSource`, confirmed in
``.venv/Lib/site-packages/discord/player.py``); the ElevenLabs SDK's `AudioInterface` gives/
wants 16-bit 16 kHz mono PCM (``elevenlabs/conversational_ai/conversation.py``). Both
directions go through simple linear interpolation (`numpy.interp`), which is more than good
enough for a voice-support bridge — not hi-fi resampling, but no aliasing horrors either at
a clean 3:1 ratio.
"""

from __future__ import annotations

import numpy as np

DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
AGENT_SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM, both sides

DISCORD_FRAME_BYTES = int(DISCORD_SAMPLE_RATE / 1000 * 20) * DISCORD_CHANNELS * SAMPLE_WIDTH_BYTES  # 3840


def _resample_mono(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if samples.size == 0 or src_rate == dst_rate:
        return samples.astype(np.float64)
    duration = samples.size / src_rate
    dst_length = max(1, round(duration * dst_rate))
    src_x = np.linspace(0.0, duration, num=samples.size, endpoint=False)
    dst_x = np.linspace(0.0, duration, num=dst_length, endpoint=False)
    return np.interp(dst_x, src_x, samples.astype(np.float64))


def _to_pcm16_bytes(samples: np.ndarray) -> bytes:
    clipped = np.clip(np.round(samples), -32768, 32767).astype("<i2")
    return clipped.tobytes()


def discord_frame_to_agent_pcm(pcm_48k_stereo: bytes) -> bytes:
    """16-bit 48 kHz stereo PCM (Discord, as decoded by the voice-recv sink) -> 16-bit
    16 kHz mono PCM (what ``AudioInterface.start``'s ``input_callback`` expects)."""
    if not pcm_48k_stereo:
        return b""
    stereo = np.frombuffer(pcm_48k_stereo, dtype="<i2")
    # Odd trailing byte/sample pairs shouldn't happen (Discord always sends whole stereo
    # frames) but a truncated buffer must not crash the bridge over a dropped tail sample.
    usable = stereo[: len(stereo) - (len(stereo) % DISCORD_CHANNELS)]
    if usable.size == 0:
        return b""
    mono = usable.reshape(-1, DISCORD_CHANNELS).astype(np.float64).mean(axis=1)
    resampled = _resample_mono(mono, DISCORD_SAMPLE_RATE, AGENT_SAMPLE_RATE)
    return _to_pcm16_bytes(resampled)


def agent_pcm_to_discord_frame(pcm_16k_mono: bytes) -> bytes:
    """16-bit 16 kHz mono PCM (``AudioInterface.output``'s ``audio`` argument) -> 16-bit
    48 kHz stereo PCM (what a ``discord.AudioSource`` must produce)."""
    if not pcm_16k_mono:
        return b""
    mono = np.frombuffer(pcm_16k_mono, dtype="<i2")
    if mono.size == 0:
        return b""
    resampled = _resample_mono(mono.astype(np.float64), AGENT_SAMPLE_RATE, DISCORD_SAMPLE_RATE)
    clipped = np.clip(np.round(resampled), -32768, 32767).astype("<i2")
    stereo = np.repeat(clipped, DISCORD_CHANNELS)
    return stereo.tobytes()
