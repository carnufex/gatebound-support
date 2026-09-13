"""Keeps Discord's DAVE end-to-end encryption OFF for the bot's voice connections.

discord.py negotiates DAVE (E2EE, ``max_dave_protocol_version > 0``) as soon as the
optional ``davey`` package is importable, which ``discord.py`` 2.7 pulls in by default.
Sending then works (discord.py encrypts what we play), but discord-ext-voice-recv has no
DAVE decryption, so every received frame is E2EE ciphertext to the Opus decoder and the
receive router dies with ``OpusError: corrupted stream`` on the first packet. Seen live
on 2026-09-13: the agent greeted the player and never heard a word back.

Announcing protocol version 0 makes the voice server fall back to transport-only
encryption for the whole channel, which both libraries handle. The gate discord.py uses
is the module flag ``discord.voice_state.has_dave`` (read by the
``VoiceConnectionState.max_dave_protocol_version`` property), so flipping it before the
first voice connect is enough.
"""
from __future__ import annotations

import discord.voice_state as _voice_state


def disable_dave() -> None:
    _voice_state.has_dave = False


def dave_disabled() -> bool:
    return not _voice_state.has_dave
