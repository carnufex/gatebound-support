"""A ``discord.AudioSource`` fed by a thread-safe queue of pre-resampled 48 kHz stereo PCM.

``discord.py`` pulls from ``read()`` once every 20 ms on its own player thread
(``.venv/Lib/site-packages/discord/player.py``: "The audio stream can be Opus encoded or
not, however if the audio stream is not Opus encoded then the audio format must be 16-bit
48KHz stereo PCM", 3840 bytes/frame). ``read()`` must never block waiting for the network —
it returns silence immediately when nothing is queued instead.
"""

from __future__ import annotations

import queue

import discord

from .resampler import DISCORD_FRAME_BYTES

SILENCE_FRAME = b"\x00" * DISCORD_FRAME_BYTES


class QueuedPCMAudioSource(discord.AudioSource):
    """Agent audio -> Discord: ``push()`` is called from the ElevenLabs SDK's own background
    thread (``AudioInterface.output``); ``read()`` is called from discord.py's player
    thread. Neither call is ever made from the asyncio loop, so this only needs to be
    thread-safe, not async-safe.
    """

    def __init__(self, *, max_queued_seconds: float = 5.0) -> None:
        max_frames = max(1, int(max_queued_seconds * 50))  # 50 frames/sec at 20ms each
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max_frames)

    def push(self, pcm_48k_stereo: bytes) -> None:
        """Splits arbitrary-length 48 kHz stereo PCM into 20 ms frames and enqueues them.
        If the queue is already full (the caller is producing audio faster than Discord can
        play it — shouldn't normally happen since the agent streams close to real-time, but
        must not be allowed to grow memory unboundedly) the oldest frame is dropped to keep
        latency bounded rather than accumulating a backlog."""
        usable_len = len(pcm_48k_stereo) - (len(pcm_48k_stereo) % DISCORD_FRAME_BYTES)
        for offset in range(0, usable_len, DISCORD_FRAME_BYTES):
            frame = pcm_48k_stereo[offset : offset + DISCORD_FRAME_BYTES]
            self._push_frame(frame)

    def _push_frame(self, frame: bytes) -> None:
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                pass

    def interrupt(self) -> None:
        """Drops all buffered agent audio — called when the player barges in mid-sentence
        (``AudioInterface.interrupt``) so the agent doesn't keep talking over them."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def read(self) -> bytes:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return SILENCE_FRAME

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.interrupt()
