"""A ``discord.AudioSource`` fed by a thread-safe queue of pre-resampled 48 kHz stereo PCM.

``discord.py`` pulls from ``read()`` once every 20 ms on its own player thread
(``.venv/Lib/site-packages/discord/player.py``: "The audio stream can be Opus encoded or
not, however if the audio stream is not Opus encoded then the audio format must be 16-bit
48KHz stereo PCM", 3840 bytes/frame). ``read()`` must never block waiting for the network —
it returns silence immediately when nothing is queued instead.
"""

from __future__ import annotations

import queue
import threading

import discord

from .resampler import DISCORD_FRAME_BYTES

SILENCE_FRAME = b"\x00" * DISCORD_FRAME_BYTES


class QueuedPCMAudioSource(discord.AudioSource):
    """Agent audio -> Discord: ``push()`` is called from the ElevenLabs SDK's own background
    thread (``AudioInterface.output``); ``read()`` is called from discord.py's player
    thread. Neither call is ever made from the asyncio loop, so this only needs to be
    thread-safe, not async-safe.
    """

    def __init__(self, *, max_queued_seconds: float = 90.0) -> None:
        # ElevenLabs streams TTS far faster than real time (a whole sentence lands in a
        # burst), so the queue must hold a full agent turn; a 5 s cap dropped the oldest
        # frames mid-sentence, which is what the 2026-09-13 test call sounded like
        # ("two tracks on top of each other"). interrupt() empties it on barge-in.
        max_frames = max(1, int(max_queued_seconds * 50))  # 50 frames/sec at 20ms each
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=max_frames)
        self._remainder = b""
        self._remainder_lock = threading.Lock()

    def push(self, pcm_48k_stereo: bytes) -> None:
        """Splits arbitrary-length 48 kHz stereo PCM into 20 ms frames and enqueues them.
        SDK chunks are not frame-aligned, so the tail that does not fill a frame is kept
        and prepended to the next push (dropping it left a gap in every chunk boundary).
        If the queue is full the oldest frame is dropped so memory stays bounded."""
        with self._remainder_lock:
            data = self._remainder + pcm_48k_stereo
            usable_len = len(data) - (len(data) % DISCORD_FRAME_BYTES)
            self._remainder = data[usable_len:]
        for offset in range(0, usable_len, DISCORD_FRAME_BYTES):
            self._push_frame(data[offset : offset + DISCORD_FRAME_BYTES])

    def flush(self) -> None:
        """Pads and enqueues the pending partial frame (end of an agent turn)."""
        with self._remainder_lock:
            tail, self._remainder = self._remainder, b""
        if tail:
            self._push_frame(tail + b"\x00" * (DISCORD_FRAME_BYTES - len(tail)))

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
        with self._remainder_lock:
            self._remainder = b""
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
