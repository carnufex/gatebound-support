"""The Discord gateway bot: voice calls (/support, /call, /hangup) and text tickets
(/ticket, persistent "Open a ticket" button).

Intents: guilds + voice_states only — no message content, per SPEC-style convention for this
service (see settings.py). Slash commands are registered per-guild (``DISCORD_GUILD_ID``) so
they appear instantly instead of waiting for Discord's up-to-an-hour global command cache.
"""

from __future__ import annotations

import asyncio
import dataclasses

import discord
from discord import app_commands
from discord.ext import voice_recv

from ..discord import DiscordClient
from ..logging import get_logger
from ..settings import Settings, enabled
from ..ticket_formatting import opening_post
from .elevenlabs_bridge import CallSession
from .opus_support import ensure_opus_loaded
from .service_client import SupportServiceClient
from .sink import SinglePlayerSink
from .state import CallRegistry
from .tickets import map_category

logger = get_logger("gatebound_support.voicebot")

PINNED_MESSAGE_PREFIX = "**Support tickets**"
PINNED_MESSAGE_BODY = (
    f"{PINNED_MESSAGE_PREFIX}\nNeed help? Click the button below to open a private ticket "
    "with staff, or join a voice channel and run `/support` to talk to the support agent."
)


@dataclasses.dataclass
class VoiceCallState:
    guild_id: int
    channel_id: int
    user_id: int
    voice_client: voice_recv.VoiceRecvClient
    session: CallSession
    sink: SinglePlayerSink
    ticket_id: str | None
    thread_id: str | None
    timeout_task: asyncio.Task | None = None
    patch_task: asyncio.Task | None = None


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

        @self.tree.command(name="support", description="Start a voice call with Gatebound Support", guild=guild)
        async def support_command(interaction: discord.Interaction) -> None:
            await self.handle_support(interaction)

        @self.tree.command(name="call", description="Start a voice call with Gatebound Support", guild=guild)
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
        voice_state = member.voice
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Join a voice channel first, then run /support.", ephemeral=True
            )
            return
        if not self.calls.try_start(guild.id, channel_id=voice_state.channel.id, user_id=member.id):
            await interaction.response.send_message(
                "The line is busy — support is already on a call in this server.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"Joining {voice_state.channel.mention}...", ephemeral=True)
        try:
            await self._start_call(guild, voice_state.channel, member)
        except Exception:
            logger.exception("failed to start voice call")
            self.calls.end(guild.id)
            await interaction.followup.send(
                "Something went wrong starting the call. Please try again.", ephemeral=True
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
        if after.channel is None or after.channel.id != call_state.channel_id:
            await self._end_call(member.guild.id, reason="player_left")

    async def _start_call(
        self, guild: discord.Guild, channel: discord.VoiceChannel, member: discord.Member
    ) -> None:
        agent_id = self.settings.ELEVENLABS_AGENT_ID
        summary = f"Voice call with {member.display_name}"

        ticket_id = await self.service_client.create_ticket(
            source="discord_voice",
            discord_user_id=str(member.id),
            discord_username=str(member),
            category="other",
            summary=summary,
        )
        thread_id: str | None = None
        if ticket_id:
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

        voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False)
        loop = asyncio.get_running_loop()

        def _on_session_ended() -> None:
            asyncio.run_coroutine_threadsafe(self._end_call(guild.id, reason="agent_ended"), loop)

        session = CallSession(
            self.settings, agent_id=agent_id, player_name=member.display_name, on_session_ended=_on_session_ended
        )
        sink = SinglePlayerSink(target_user_id=member.id, on_player_audio=session.send_player_audio)

        call_state = VoiceCallState(
            guild_id=guild.id,
            channel_id=channel.id,
            user_id=member.id,
            voice_client=voice_client,
            session=session,
            sink=sink,
            ticket_id=ticket_id,
            thread_id=thread_id,
        )
        self._active_calls[guild.id] = call_state

        voice_client.listen(sink)
        voice_client.play(session.audio_source)
        session.start()

        call_state.timeout_task = asyncio.create_task(self._call_timeout(guild.id))
        if ticket_id and thread_id:
            call_state.patch_task = asyncio.create_task(
                self._patch_conversation_id(ticket_id, thread_id, str(guild.id), str(member.id), session)
            )

        logger.info(
            "voice call started",
            extra={
                "fields": {
                    "guild_id": guild.id,
                    "channel_id": channel.id,
                    "ticket_id": ticket_id,
                    "thread_id": thread_id,
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
        if call_state.timeout_task is not None:
            call_state.timeout_task.cancel()
        call_state.session.end()
        try:
            call_state.voice_client.stop_listening()
        except Exception:
            logger.exception("stop_listening failed")
        try:
            await call_state.voice_client.disconnect(force=True)
        except Exception:
            logger.exception("voice disconnect failed")
        self.calls.end(guild_id)
        logger.info(
            "voice call ended",
            extra={
                "fields": {
                    "guild_id": guild_id,
                    "reason": reason,
                    "ticket_id": call_state.ticket_id,
                    "conversation_id": call_state.session.conversation_id,
                }
            },
        )
