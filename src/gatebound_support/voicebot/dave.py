"""DAVE (Discord end-to-end encrypted voice) support for the RECEIVE side.

Discord now requires DAVE in voice channels: announcing protocol version 0 gets the voice
websocket closed with 4017 "This channel requires a client supporting E2EE via the DAVE
Protocol" (seen live 2026-09-13, join/disconnect loop). discord.py 2.7 + ``davey`` handle
the MLS session and encrypt what we SEND (``VoiceClient._get_voice_packet`` ->
``dave_session.encrypt_opus``), but discord-ext-voice-recv knows nothing about DAVE: after
transport decryption it hands the still-E2EE-encrypted frame to the Opus decoder, which
fails with ``OpusError: corrupted stream`` and kills the receive router thread.

``install_dave_receive()`` patches two points in discord-ext-voice-recv:

- ``PacketRouter.feed_rtp``: before a packet reaches a decoder, decrypt its payload with
  the voice client's ``dave_session`` (``davey.DaveSession.decrypt(user_id, audio, frame)``)
  once the session is ready (``VoiceConnectionState.can_encrypt``). The sender is resolved
  from the ssrc; a packet whose sender is not yet known cannot be decrypted and is dropped.
- ``PacketDecoder._decode_packet``: an ``OpusError`` yields one frame of silence instead of
  propagating, so a single undecodable packet (transition edge, unknown sender) can never
  kill the router again.
"""
from __future__ import annotations

import time
from typing import Any

import discord.voice_state as _voice_state
from discord.ext.voice_recv import opus as _vr_opus
from discord.ext.voice_recv import router as _vr_router
from discord.opus import OpusError

from ..logging import get_logger

try:
    import davey
except ImportError:  # pragma: no cover - davey ships with discord.py >= 2.7
    davey = None

logger = get_logger("gatebound_support.voicebot.dave")

SILENCE_PCM = b"\x00" * 3840  # one 20 ms 48 kHz stereo frame
_LOG_INTERVAL_SECONDS = 10.0
_last_log: dict[str, float] = {}


def dave_available() -> bool:
    """True when discord.py will negotiate DAVE (davey importable)."""
    return davey is not None and bool(getattr(_voice_state, "has_dave", False))


def _log_throttled(key: str, message: str, **fields: Any) -> None:
    now = time.monotonic()
    if now - _last_log.get(key, 0.0) >= _LOG_INTERVAL_SECONDS:
        _last_log[key] = now
        logger.warning(message, extra={"fields": fields})


def dave_session_for(voice_client: Any) -> Any | None:
    """The ready DAVE session of a voice client, or None (not negotiated / not ready)."""
    connection = getattr(voice_client, "_connection", None)
    session = getattr(connection, "dave_session", None)
    if session is None or not getattr(connection, "can_encrypt", False):
        return None
    return session


def decrypt_payload(voice_client: Any, ssrc: int, payload: bytes) -> bytes | None:
    """DAVE-decrypts one RTP payload. Returns the payload unchanged when no DAVE session is
    ready (transport-only phase), the plaintext when decryption works, and None when the
    packet must be dropped (sender unknown or decryption failed)."""
    session = dave_session_for(voice_client)
    if session is None:
        return payload
    user_id = voice_client._get_id_from_ssrc(ssrc)
    if user_id is None:
        _log_throttled("unknown_ssrc", "dropping E2EE packet from unknown ssrc", ssrc=ssrc)
        return None
    try:
        return session.decrypt(int(user_id), davey.MediaType.audio, bytes(payload))
    except Exception as exc:  # noqa: BLE001 - davey raises plain exceptions; a bad frame must never kill the router
        _log_throttled("decrypt_failed", "DAVE decrypt failed", ssrc=ssrc, error=str(exc)[:120])
        return None


def install_dave_receive() -> None:
    """Idempotent monkeypatch of discord-ext-voice-recv (see module docstring)."""
    if getattr(_vr_router.PacketRouter, "_gb_dave_patched", False):
        return

    original_feed_rtp = _vr_router.PacketRouter.feed_rtp

    def feed_rtp(self: Any, packet: Any) -> None:
        payload = getattr(packet, "decrypted_data", None)
        if payload:
            voice_client = self.reader.voice_client
            plaintext = decrypt_payload(voice_client, packet.ssrc, payload)
            if plaintext is None:
                return
            packet.decrypted_data = plaintext
        original_feed_rtp(self, packet)

    original_decode = _vr_opus.PacketDecoder._decode_packet

    def _decode_packet(self: Any, packet: Any) -> tuple[Any, bytes]:
        try:
            return original_decode(self, packet)
        except OpusError as exc:
            _log_throttled("opus_error", "Opus decode failed; substituting silence", error=str(exc)[:80])
            return packet, SILENCE_PCM

    _vr_router.PacketRouter.feed_rtp = feed_rtp
    _vr_opus.PacketDecoder._decode_packet = _decode_packet
    _vr_router.PacketRouter._gb_dave_patched = True
    logger.info("DAVE receive-side decryption installed", extra={"fields": {"davey": dave_available()}})
