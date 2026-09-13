from __future__ import annotations

import math
import struct

from gatebound_support.voicebot.resampler import (
    AGENT_SAMPLE_RATE,
    DISCORD_CHANNELS,
    DISCORD_FRAME_BYTES,
    DISCORD_SAMPLE_RATE,
    agent_pcm_to_discord_frame,
    discord_frame_to_agent_pcm,
)


def _sine_pcm16(*, rate: int, channels: int, freq: float, seconds: float, amplitude: int = 10000) -> bytes:
    n = int(rate * seconds)
    samples: list[int] = []
    for i in range(n):
        value = int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        for _ in range(channels):
            samples.append(value)
    return struct.pack(f"<{len(samples)}h", *samples)


def test_discord_frame_bytes_matches_20ms_stereo_48k() -> None:
    # 48000 samples/sec * 0.02 sec * 2 channels * 2 bytes/sample
    assert DISCORD_FRAME_BYTES == 3840


def test_downmix_and_downsample_length_matches_target_rate() -> None:
    seconds = 0.5
    pcm = _sine_pcm16(rate=DISCORD_SAMPLE_RATE, channels=DISCORD_CHANNELS, freq=220.0, seconds=seconds)
    agent_pcm = discord_frame_to_agent_pcm(pcm)

    expected_samples = int(AGENT_SAMPLE_RATE * seconds)
    actual_samples = len(agent_pcm) // 2  # 16-bit mono
    assert abs(actual_samples - expected_samples) <= 2


def test_upsample_to_stereo_48k_length_and_channel_duplication() -> None:
    seconds = 0.5
    pcm = _sine_pcm16(rate=AGENT_SAMPLE_RATE, channels=1, freq=220.0, seconds=seconds)
    discord_pcm = agent_pcm_to_discord_frame(pcm)

    expected_samples = int(DISCORD_SAMPLE_RATE * seconds) * DISCORD_CHANNELS
    actual_samples = len(discord_pcm) // 2
    assert abs(actual_samples - expected_samples) <= 2 * DISCORD_CHANNELS

    # channels must be identical (mono duplicated into stereo, not independently generated)
    values = struct.unpack(f"<{actual_samples}h", discord_pcm[: actual_samples * 2])
    left = values[0::2]
    right = values[1::2]
    assert left == right


def test_amplitude_sanity_round_trip_stays_in_range() -> None:
    seconds = 0.2
    amplitude = 8000
    pcm = _sine_pcm16(
        rate=DISCORD_SAMPLE_RATE, channels=DISCORD_CHANNELS, freq=440.0, seconds=seconds, amplitude=amplitude
    )
    agent_pcm = discord_frame_to_agent_pcm(pcm)
    back = agent_pcm_to_discord_frame(agent_pcm)

    values = struct.unpack(f"<{len(back) // 2}h", back)
    assert max(values) <= amplitude * 1.2
    assert min(values) >= -amplitude * 1.2
    # not silence
    assert max(abs(v) for v in values) > amplitude * 0.3


def test_empty_input_returns_empty_output() -> None:
    assert discord_frame_to_agent_pcm(b"") == b""
    assert agent_pcm_to_discord_frame(b"") == b""


def test_silence_stays_silence() -> None:
    silence = b"\x00" * DISCORD_FRAME_BYTES
    agent_pcm = discord_frame_to_agent_pcm(silence)
    assert agent_pcm == b"\x00" * len(agent_pcm)
