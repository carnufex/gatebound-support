"""Wires the ElevenLabs Conversational AI SDK's ``Conversation`` to a Discord voice call.

Verified against the installed ``elevenlabs==2.68.0`` package
(``.venv/Lib/site-packages/elevenlabs/conversational_ai/conversation.py``):

- ``Conversation`` (not ``AsyncConversation``) fits here: it runs its own background thread
  (``start_session`` -> ``threading.Thread(target=self._run, ...)``) and drives the
  ``AudioInterface`` abstract methods (``start``, ``stop``, ``output``, ``interrupt``)
  synchronously from that thread — a natural match for discord.py's own audio threads
  (the voice-recv sink's receive thread, and the AudioSource player thread), none of which
  run on the asyncio event loop either.
- ``requires_auth=True`` is enough to get a signed URL automatically: ``BaseConversation.
  _get_signed_url`` (called from ``start_session`` when ``requires_auth`` is set) calls
  ``self.client.conversational_ai.conversations.get_signed_url(agent_id=...)`` itself using
  the API key on the ``ElevenLabs`` client — no need to hand-roll the signed-URL request.
- The conversation id becomes available as soon as the server's
  ``conversation_initiation_metadata`` event arrives (``self._conversation_id = event[
  "conversation_id"]``, conversation.py:580). There is no public getter that doesn't block
  (``wait_for_session_end`` blocks until the call is over), so ``wait_for_conversation_id``
  below polls the same attribute the SDK sets internally.
- Hangup detection: when the WebSocket closes for any reason (the agent's own ``end_call``
  tool included), ``_run``'s receive loop catches ``ConnectionClosedOK`` and calls
  ``self.end_session()`` itself (conversation.py:951-952), which in turn calls our
  ``callback_end_session`` — so agent-initiated hangups reach ``on_session_ended`` the same
  way an explicit ``CallSession.end()`` does, with no separate "listen for end_call" wiring
  needed.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from elevenlabs.conversational_ai.conversation import (
    AudioInterface,
    Conversation,
    ConversationInitiationData,
)

from elevenlabs import ElevenLabs

from ..logging import get_logger
from ..settings import Settings
from .audio_source import QueuedPCMAudioSource
from .resampler import agent_pcm_to_discord_frame

logger = get_logger("gatebound_support.voicebot.elevenlabs_bridge")

CONVERSATION_ID_POLL_TIMEOUT_SECONDS = 5.0
CONVERSATION_ID_POLL_INTERVAL_SECONDS = 0.1


class DiscordAudioInterface(AudioInterface):
    """The ``AudioInterface`` the ElevenLabs SDK drives. Player audio flows in the other
    direction (Discord -> here -> the SDK's ``input_callback``) via ``send_player_audio``,
    called by the voice-recv sink; it is not part of the ``AudioInterface`` contract itself.
    """

    def __init__(self, audio_source: QueuedPCMAudioSource) -> None:
        self._audio_source = audio_source
        self._input_callback: Callable[[bytes], None] | None = None

    def start(self, input_callback: Callable[[bytes], None]) -> None:
        self._input_callback = input_callback

    def stop(self) -> None:
        self._input_callback = None
        self._audio_source.interrupt()

    def output(self, audio: bytes) -> None:
        self._audio_source.push(agent_pcm_to_discord_frame(audio))

    def interrupt(self) -> None:
        self._audio_source.interrupt()

    def send_player_audio(self, pcm_16k_mono: bytes) -> None:
        callback = self._input_callback
        if callback is not None and pcm_16k_mono:
            callback(pcm_16k_mono)


class CallSession:
    """One player's voice call with the support agent. Wraps the SDK's sync ``Conversation``
    so the rest of the voicebot only deals with plain audio bytes and a couple of
    thread-safe accessors."""

    def __init__(
        self,
        settings: Settings,
        *,
        agent_id: str,
        player_name: str,
        on_session_ended: Callable[[], None] | None = None,
    ) -> None:
        self.audio_source = QueuedPCMAudioSource()
        self._audio_interface = DiscordAudioInterface(self.audio_source)
        self._on_session_ended = on_session_ended
        self._ended = threading.Event()
        client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
        self._conversation = Conversation(
            client,
            agent_id,
            requires_auth=True,
            audio_interface=self._audio_interface,
            config=ConversationInitiationData(
                dynamic_variables={
                    "player_name": player_name,
                    "logged_in": "no",
                    "identity_token": "",
                }
            ),
            callback_end_session=self._handle_session_ended,
        )

    def start(self) -> None:
        self._conversation.start_session()

    def send_player_audio(self, pcm_16k_mono: bytes) -> None:
        self._audio_interface.send_player_audio(pcm_16k_mono)

    def interrupt(self) -> None:
        self._audio_interface.interrupt()

    def end(self) -> None:
        """Idempotent: safe to call even if the SDK already ended the session itself (agent
        hangup) — ``end_session`` just re-sets state that's already set."""
        if not self._ended.is_set():
            self._conversation.end_session()

    def _handle_session_ended(self) -> None:
        self._ended.set()
        if self._on_session_ended is not None:
            self._on_session_ended()

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    @property
    def conversation_id(self) -> str | None:
        return self._conversation._conversation_id

    def wait_for_conversation_id(
        self, timeout: float = CONVERSATION_ID_POLL_TIMEOUT_SECONDS
    ) -> str | None:
        """Blocks (on a background thread — never call from the asyncio loop) until the
        conversation id is known or ``timeout`` elapses. Used right after ``start()`` so the
        ticket thread's ``conversation_id`` can be PATCHed onto the internal API before the
        call ends, which is what lets the post-call webhook find the thread."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            conversation_id = self.conversation_id
            if conversation_id:
                return conversation_id
            if self._ended.is_set():
                return self.conversation_id
            time.sleep(CONVERSATION_ID_POLL_INTERVAL_SECONDS)
        return self.conversation_id
