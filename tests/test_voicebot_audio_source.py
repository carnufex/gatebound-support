from __future__ import annotations

from gatebound_support.voicebot.audio_source import SILENCE_FRAME, QueuedPCMAudioSource
from gatebound_support.voicebot.resampler import DISCORD_FRAME_BYTES


def test_read_returns_silence_when_empty() -> None:
    source = QueuedPCMAudioSource()
    frame = source.read()
    assert frame == SILENCE_FRAME
    assert len(frame) == DISCORD_FRAME_BYTES


def test_is_opus_false() -> None:
    assert QueuedPCMAudioSource().is_opus() is False


def test_push_splits_into_exact_frames_and_read_returns_them_in_order() -> None:
    source = QueuedPCMAudioSource()
    frame_a = bytes([1]) * DISCORD_FRAME_BYTES
    frame_b = bytes([2]) * DISCORD_FRAME_BYTES
    source.push(frame_a + frame_b)

    assert source.read() == frame_a
    assert source.read() == frame_b
    # queue now empty -> silence
    assert source.read() == SILENCE_FRAME


def test_push_drops_partial_trailing_frame() -> None:
    source = QueuedPCMAudioSource()
    full_frame = bytes([9]) * DISCORD_FRAME_BYTES
    partial = bytes([5]) * (DISCORD_FRAME_BYTES // 2)
    source.push(full_frame + partial)

    assert source.read() == full_frame
    assert source.read() == SILENCE_FRAME


def test_interrupt_clears_queued_audio() -> None:
    source = QueuedPCMAudioSource()
    source.push(bytes([7]) * DISCORD_FRAME_BYTES * 3)
    source.interrupt()
    assert source.read() == SILENCE_FRAME


def test_queue_bounded_oldest_frame_dropped_when_full() -> None:
    source = QueuedPCMAudioSource(max_queued_seconds=0.02)  # max_frames == 1
    frame_old = bytes([1]) * DISCORD_FRAME_BYTES
    frame_new = bytes([2]) * DISCORD_FRAME_BYTES
    source.push(frame_old)
    source.push(frame_new)

    # only the newest frame should remain
    assert source.read() == frame_new
    assert source.read() == SILENCE_FRAME


def test_cleanup_clears_queue() -> None:
    source = QueuedPCMAudioSource()
    source.push(bytes([3]) * DISCORD_FRAME_BYTES)
    source.cleanup()
    assert source.read() == SILENCE_FRAME
