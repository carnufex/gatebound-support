"""The Discord gateway bot: voice calls (/support, /call, /hangup) and text tickets
(/ticket, persistent "Open a ticket" button).

Intents: guilds + voice_states only — no message content, per SPEC-style convention for this
service (see settings.py). Slash commands are registered per-guild (``DISCORD_GUILD_ID``) so
they appear instantly instead of waiting for Discord's up-to-an-hour global command cache.

Voice calls run in a private, per-call voice channel (SPEC change: a support call cannot be
overheard or joined by other guild members) rather than the bot joining whatever channel the
player happens to be in — see ``_create_private_voice_channel`` and ``channels.py``. That
needs the bot to hold Manage Channels + Manage Roles on the guild permanently (documented in
the README), in addition to Connect/Speak/Use Voice Activity for the call itself and
(optional, only used when present) Move Members to pull an already-in-voice player straight
into their new private channel.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

import discord
from discord import app_commands
from discord.ext import voice_recv

from ..discord import DiscordClient
from ..logging import get_logger
from ..settings import Settings, enabled
from ..ticket_formatting import opening_post
from .channels import (
    build_support_channel_name,
    build_voice_channel_overwrites,
    format_call_duration,
    is_support_channel_name,
    select_staff_roles,
)
from .elevenlabs_bridge import CallSession
from .opus_support import ensure_opus_loaded
from .service_client import SupportServiceClient
from .sink import SinglePlayerSink
from .state import JOIN_TIMEOUT_SECONDS, CallRegistry, PendingCall
from .tickets import map_category

logger = get_logger("gatebound_support.voicebot")

PINNED_MESSAGE_PREFIX = "**Support tickets**"
PINNED_MESSAGE_BODY = (
    f"{PINNED_MESSAGE_PREFIX}\nNeed help? Click the button below to open a private ticket "
    "with staff, or run /support to start a private voice call with the support agent "
    "(a private voice channel is created just for you)."
)


@dataclasses.dataclass
class VoiceCallState:
    """One ``/support`` call, from channel creation through hangup. ``pending`` owns the
    "waiting for join" / "active" phase and the 2-minute expiry (state.PendingCall, pure and
    separately unit-tested); everything else here is the live Discord/ElevenLabs handles
    that only exist once connected, plus the bookkeeping needed to end the call cleanly."""

    pending: PendingCall
    ticket_id: str | None
    thread_id: str | None
    voice_client: voice_recv.VoiceRecvClient | None = None
    session: CallSession | None = None
    sink: SinglePlayerSink | None = None
    started_at: float | None = None
    join_timeout_task: asyncio.Task | None = None
    call_timeout_task: asyncio.Task | None = None
    patch_task: asyncio.Task | None = None

    @property
    def guild_id(self) -> int:
        return self.pending.guild_id

    @property
    def channel_id(self) -> int:
        return self.pending.channel_id

    @property
    def user_id(self) -> int:
        return self.pending.user_id


class TicketModal(discord.ui.Modal, title="Open a support ticket"):
    """A category select isn't available in modals (Discord limitation), so the category is
    free text mapped to one of the five SPEC categories by ``voicebot.tickets.map_category``.
    """

    category_field = discord.ui.Label(
        text="What is it about? (account / bug / payment / report player / other)",
        component=discord.ui.TextInput(style=discord.TextStyle.short, max_length=100, required=True),
    )
    description_field = discord.ui.Label(
        text="Describe the problem",
        component=discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000, required=True),
    )

    def __init__(self, bot: VoiceBot) -> None:
        super().__init__()
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        category = map_category(self.category_field.component.value)
        summary = self.description_field.component.value.strip()[:1000] or "(no description given)"
        member = interaction.user
        bot = self._bot

        ticket_id = await bot.service_client.create_ticket(
            source="discord_text",
            discord_user_id=str(member.id),
            discord_username=str(member),
            category=category,
            summary=summary,
        )
        if not ticket_id:
            await interaction.followup.send(
                "Something went wrong creating your ticket. Please try again in a moment.", ephemeral=True
            )
            return

        content = opening_post(
            ticket_id=ticket_id,
            category=category,
            priority="normal",
            summary=summary,
            account_name=str(member),
            conversation_id=None,
            discord_user_id=str(member.id),
        )
        thread_id = await bot.discord_client.create_ticket_thread(
            ticket_id=ticket_id, title=f"#{ticket_id} {summary[:80]}", content=content
        )
        if not thread_id:
            await interaction.followup.send(
                f"Ticket {ticket_id} was created, but I couldn't open a Discord thread. "
                "Staff will follow up.",
                ephemeral=True,
            )
            return
        await bot.discord_client.add_thread_member(thread_id=thread_id, user_id=str(member.id))
        await bot.service_client.patch_ticket(
            ticket_id,
            thread_id=thread_id,
            guild_id=str(interaction.guild_id) if interaction.guild_id else "",
            discord_user_id=str(member.id),
        )
        url = f"https://discord.com/channels/{interaction.guild_id}/{thread_id}"
        await interaction.followup.send(f"Ticket opened: {url}", ephemeral=True)


class TicketButtonView(discord.ui.View):
    """Persistent (``timeout=None``) view carrying the "Open a ticket" button attached to the
    pinned instructions message. Registered with ``bot.add_view`` on startup (custom_id
    ``gb_open_ticket``, fixed rather than generated) so it keeps working across bot restarts
    without needing to re-send the message."""

    def __init__(self, bot: VoiceBot) -> None:
        super().__init__(timeout=None)
        self._bot = bot

    @discord.ui.button(label="Open a ticket", style=discord.ButtonStyle.primary, custom_id="gb_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._bot.offer_ticket_modal(interaction)


class VoiceBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.discord_client = DiscordClient(settings)
        self.service_client = SupportServiceClient(settings)
        self.calls = CallRegistry()
        self._guild_id = int(settings.DISCORD_GUILD_ID)
        self._support_channel_id = int(settings.DISCORD_SUPPORT_CHANNEL_ID)
        self._active_calls: dict[int, VoiceCallState] = {}

    # ---- lifecycle ----

    async def setup_hook(self) -> None:
        if not ensure_opus_loaded():
            logger.warning("libopus could not be loaded; voice will not work")

        guild = discord.Object(id=self._guild_id)

        @self.tree.command(name="support", description="Start a private voice call with Gatebound Support", guild=guild)
        async def support_command(interaction: discord.Interaction) -> None:
            await self.handle_support(interaction)

        @self.tree.command(name="call", description="Start a private voice call with Gatebound Support", guild=guild)
        async def call_command(interaction: discord.Interaction) -> None:
            await self.handle_support(interaction)

        @self.tree.command(
            name="hangup", description="End the current Gatebound Support voice call", guild=guild
        )
        async def hangup_command(interaction: discord.Interaction) -> None:
            await self.handle_hangup(interaction)

        @self.tree.command(name="ticket", description="Open a Gatebound support ticket", guild=guild)
        async def ticket_command(interaction: discord.Interaction) -> None:
            await self.offer_ticket_modal(interaction)

        self.add_view(TicketButtonView(self))
        await self.tree.sync(guild=guild)
        logger.info("slash commands synced", extra={"fields": {"guild_id": self._guild_id}})

    async def on_ready(self) -> None:
        logger.info("voicebot ready", extra={"fields": {"user": str(self.user)}})
        try:
            await self._ensure_pinned_ticket_message()
        except discord.DiscordException:
            logger.exception("failed to ensure pinned ticket message")
        try:
            await self._cleanup_stale_support_channels()
        except discord.DiscordException:
            logger.exception("failed to clean up stale support voice channels")

    async def close(self) -> None:
        await self.discord_client.aclose()
        await self.service_client.aclose()
        await super().close()

    async def _ensure_pinned_ticket_message(self) -> None:
        channel = self.get_channel(self._support_channel_id) or await self.fetch_channel(
            self._support_channel_id
        )
        view = TicketButtonView(self)
        async for message in channel.history(limit=50):
            if message.author.id == self.user.id and message.content.startswith(PINNED_MESSAGE_PREFIX):
                await message.edit(content=PINNED_MESSAGE_BODY, view=view)
                return
        await channel.send(PINNED_MESSAGE_BODY, view=view)

    async def _cleanup_stale_support_channels(self) -> None:
        """Deletes leftover ``support-*`` voice channels from a previous run (crash/restart
        mid-call). There's no "who created this channel" API to check, so the two available
        signals are used instead: the name prefix, and the bot's own id present in the
        channel's permission overwrites (every channel this bot creates grants itself
        Connect explicitly — see ``build_voice_channel_overwrites``). Only empty channels are
        touched, so a call that's still genuinely in progress (this check only really matters
        right after a restart) is never disturbed."""
        guild = self.get_guild(self._guild_id)
        if guild is None:
            return
        bot_id = self.user.id if self.user else None
        for channel in list(guild.voice_channels):
            if not is_support_channel_name(channel.name):
                continue
            if channel.members:
                continue
            overwrite_ids = {target.id for target in channel.overwrites}
            if bot_id not in overwrite_ids:
                continue
            try:
                await channel.delete(reason="Stale Gatebound support voice channel from a previous run")
                logger.info("deleted stale support channel", extra={"fields": {"channel_id": channel.id}})
            except discord.DiscordException:
                logger.exception(
                    "failed to delete stale support channel", extra={"fields": {"channel_id": channel.id}}
                )

    # ---- text tickets ----

    async def offer_ticket_modal(self, interaction: discord.Interaction) -> None:
        existing = await self.service_client.get_open_ticket(str(interaction.user.id))
        if existing and existing.get("thread_id"):
            url = f"https://discord.com/channels/{self.settings.DISCORD_GUILD_ID}/{existing['thread_id']}"
            await interaction.response.send_message(
                f"You already have an open ticket: {url}", ephemeral=True
            )
            return
        await interaction.response.send_modal(TicketModal(self))

    # ---- voice calls ----

    async def handle_support(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if not enabled(self.settings.ELEVENLABS_API_KEY) or not enabled(self.settings.ELEVENLABS_AGENT_ID):
            await interaction.response.send_message("Voice support isn't configured right now.", ephemeral=True)
            return
        # Reserve the guild's one-call slot before doing anything else (channel_id filled in
        # once the channel exists) so two /support calls racing each other can't both pass.
        if not self.calls.try_start(guild.id, channel_id=0, user_id=member.id):
            await interaction.response.send_message(
                "The line is busy — support is already on a call in this server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await self._create_private_voice_channel(guild, member)
        except Exception:
            logger.exception("failed to create private voice channel")
            self.calls.end(guild.id)
            await interaction.followup.send(
                "Something went wrong starting the call. Please try again.", ephemeral=True
            )
            return
        self.calls.set_channel_id(guild.id, channel.id)

        ticket_id, thread_id = await self._open_voice_ticket(member)

        call_state = VoiceCallState(
            pending=PendingCall(
                guild_id=guild.id, user_id=member.id, channel_id=channel.id, created_at=time.monotonic()
            ),
            ticket_id=ticket_id,
            thread_id=thread_id,
        )
        call_state.join_timeout_task = asyncio.create_task(self._join_timeout(guild.id))
        self._active_calls[guild.id] = call_state

        # Best-effort convenience: if the player is already in a voice channel and the bot
        # has Move Members, pull them straight in instead of making them click the link.
        voice_state = member.voice
        if (
            voice_state is not None
            and voice_state.channel is not None
            and guild.me is not None
            and guild.me.guild_permissions.move_members
        ):
            try:
                await member.move_to(channel, reason="Gatebound support call")
            except discord.DiscordException:
                logger.exception("failed to move member into private support channel")

        logger.info(
            "voice channel created",
            extra={"fields": {"guild_id": guild.id, "channel_id": channel.id, "ticket_id": ticket_id}},
        )
        await interaction.followup.send(
            f"{channel.mention} — join it and I'll pick up.", ephemeral=True
        )

    async def handle_hangup(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        if guild.id not in self._active_calls:
            await interaction.response.send_message("There's no active support call to hang up.", ephemeral=True)
            return
        await interaction.response.send_message("Hanging up.", ephemeral=True)
        await self._end_call(guild.id, reason="hangup_command")

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        call_state = self._active_calls.get(member.guild.id)
        if call_state is None or member.id != call_state.user_id:
            return
        if call_state.pending.is_waiting():
            if after.channel is not None and after.channel.id == call_state.channel_id:
                await self._activate_call(call_state)
            return
        # Active call: the player leaving their private channel ends it.
        if after.channel is None or after.channel.id != call_state.channel_id:
            await self._end_call(member.guild.id, reason="player_left")

    # ---- helpers ----

    async def _create_private_voice_channel(
        self, guild: discord.Guild, member: discord.Member
    ) -> discord.VoiceChannel:
        """A channel only the invoking player, the bot, and Administrator/Manage-Guild roles
        can see or join — SPEC: a support call must not be overhearable or joinable by other
        members. Requires the bot to hold Manage Channels + Manage Roles on the guild."""
        support_text_channel = guild.get_channel(self._support_channel_id)
        category = support_text_channel.category if isinstance(support_text_channel, discord.TextChannel) else None
        bot_member = guild.me
        overwrites = build_voice_channel_overwrites(
            everyone_role=guild.default_role,
            member=member,
            bot_member=bot_member,
            staff_roles=select_staff_roles(guild.roles),
        )
        name = build_support_channel_name(member.display_name)
        return await guild.create_voice_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            reason=f"Gatebound support call for {member}",
        )

    async def _open_voice_ticket(self, member: discord.Member) -> tuple[str | None, str | None]:
        summary = f"Voice call with {member.display_name}"
        ticket_id = await self.service_client.create_ticket(
            source="discord_voice",
            discord_user_id=str(member.id),
            discord_username=str(member),
            category="other",
            summary=summary,
        )
        if not ticket_id:
            return None, None
        content = opening_post(
            ticket_id=ticket_id,
            category="other",
            priority="normal",
            summary=summary,
            account_name=str(member),
            conversation_id=None,
            discord_user_id=str(member.id),
            footer="Live voice call in progress; the transcript is posted here when the call ends.",
        )
        thread_id = await self.discord_client.create_ticket_thread(
            ticket_id=ticket_id, title=f"#{ticket_id} {summary}"[:100], content=content
        )
        if thread_id:
            await self.discord_client.add_thread_member(thread_id=thread_id, user_id=str(member.id))
        return ticket_id, thread_id

    async def _activate_call(self, call_state: VoiceCallState) -> None:
        if call_state.join_timeout_task is not None:
            call_state.join_timeout_task.cancel()

        guild = self.get_guild(call_state.guild_id)
        channel = guild.get_channel(call_state.channel_id) if guild else None
        if guild is None or not isinstance(channel, discord.VoiceChannel):
            logger.warning(
                "cannot activate call: channel gone", extra={"fields": {"guild_id": call_state.guild_id}}
            )
            await self._end_call(call_state.guild_id, reason="channel_missing")
            return

        member = guild.get_member(call_state.user_id)
        player_name = member.display_name if member is not None else "Player"

        voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)
        loop = asyncio.get_running_loop()

        def _on_session_ended() -> None:
            asyncio.run_coroutine_threadsafe(self._end_call(call_state.guild_id, reason="agent_ended"), loop)

        session = CallSession(
            self.settings,
            agent_id=self.settings.ELEVENLABS_AGENT_ID,
            player_name=player_name,
            on_session_ended=_on_session_ended,
        )
        sink = SinglePlayerSink(target_user_id=call_state.user_id, on_player_audio=session.send_player_audio)

        call_state.pending.mark_active()
        call_state.voice_client = voice_client
        call_state.session = session
        call_state.sink = sink
        call_state.started_at = time.monotonic()

        voice_client.listen(sink)
        voice_client.play(session.audio_source)
        session.start()

        call_state.call_timeout_task = asyncio.create_task(self._call_timeout(call_state.guild_id))
        if call_state.ticket_id and call_state.thread_id:
            call_state.patch_task = asyncio.create_task(
                self._patch_conversation_id(
                    call_state.ticket_id,
                    call_state.thread_id,
                    str(call_state.guild_id),
                    str(call_state.user_id),
                    session,
                )
            )

        logger.info(
            "voice call activated",
            extra={
                "fields": {
                    "guild_id": call_state.guild_id,
                    "channel_id": call_state.channel_id,
                    "ticket_id": call_state.ticket_id,
                }
            },
        )

    async def _patch_conversation_id(
        self, ticket_id: str, thread_id: str, guild_id: str, discord_user_id: str, session: CallSession
    ) -> None:
        conversation_id = await asyncio.to_thread(session.wait_for_conversation_id)
        if conversation_id:
            await self.service_client.patch_ticket(
                ticket_id,
                thread_id=thread_id,
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                conversation_id=conversation_id,
            )

    async def _join_timeout(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(JOIN_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        call_state = self._active_calls.get(guild_id)
        if call_state is None or not call_state.pending.is_waiting():
            return
        await self._end_call(guild_id, reason="join_timeout")

    async def _call_timeout(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(self.settings.VOICE_MAX_MINUTES * 60)
        except asyncio.CancelledError:
            return
        await self._end_call(guild_id, reason="timeout")

    async def _end_call(self, guild_id: int, *, reason: str) -> None:
        call_state = self._active_calls.pop(guild_id, None)
        if call_state is None:
            return
        if call_state.join_timeout_task is not None:
            call_state.join_timeout_task.cancel()
        if call_state.call_timeout_task is not None:
            call_state.call_timeout_task.cancel()
        if call_state.session is not None:
            call_state.session.end()
        if call_state.voice_client is not None:
            try:
                call_state.voice_client.stop_listening()
            except Exception:
                logger.exception("stop_listening failed")
            try:
                await call_state.voice_client.disconnect(force=True)
            except Exception:
                logger.exception("voice disconnect failed")

        guild = self.get_guild(guild_id)
        channel = guild.get_channel(call_state.channel_id) if guild else None
        if channel is not None:
            try:
                await channel.delete(reason=f"Gatebound support call ended ({reason})")
            except discord.DiscordException:
                logger.exception("failed to delete temporary voice channel")

        self.calls.end(guild_id)

        if call_state.thread_id:
            if reason == "join_timeout":
                note = "Call not started — nobody joined the voice channel within 2 minutes."
            else:
                note = (
                    f"Call ended ({format_call_duration(time.monotonic() - call_state.started_at)})."
                    if call_state.started_at is not None
                    else "Call ended."
                )
            await self.discord_client.post_message(thread_id=call_state.thread_id, content=note)

        logger.info(
            "voice call ended",
            extra={
                "fields": {
                    "guild_id": guild_id,
                    "reason": reason,
                    "ticket_id": call_state.ticket_id,
                    "conversation_id": call_state.session.conversation_id if call_state.session else None,
                }
            },
        )
