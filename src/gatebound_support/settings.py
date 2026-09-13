"""Configuration, loaded from the environment (see docs/SPEC.md sections 1 and 8).

Convention (same as oncall-voice-copilot): a secret with the literal value ``unset`` or an
empty string means "integration disabled". The service must start and stay healthy with
every integration disabled; affected features degrade with an explicit message.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def enabled(value: str | None) -> bool:
    """True if a secret/config value is actually set (not empty, not the literal "unset")."""
    return bool(value) and value.strip().lower() != "unset"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- runtime ---
    PORT: int = 8080
    DATA_DIR: str = "./data"
    LOG_LEVEL: str = "INFO"
    PUBLIC_BASE_URL: str = "unset"
    WEB_API_BASE_URL: str = "unset"
    WEB_PUBLIC_URL: str = "unset"

    # --- §1 trust boundaries and secrets ---
    SUPPORT_API_TOKEN: str = "unset"
    SUPPORT_IDENTITY_SECRET: str = "unset"
    MCP_SECRET: str = "unset"
    ELEVENLABS_API_KEY: str = "unset"
    ELEVENLABS_WEBHOOK_SECRET: str = "unset"
    ELEVENLABS_AGENT_ID: str = "unset"
    DISCORD_CLIENT_ID: str = "unset"
    DISCORD_CLIENT_SECRET: str = "unset"
    DISCORD_BOT_TOKEN: str = "unset"
    DISCORD_GUILD_ID: str = "unset"
    DISCORD_SUPPORT_CHANNEL_ID: str = "unset"
    DISCORD_STAFF_WEBHOOK_URL: str = "unset"

    @property
    def discord_enabled(self) -> bool:
        return enabled(self.DISCORD_CLIENT_ID) and enabled(self.DISCORD_CLIENT_SECRET) and enabled(self.DISCORD_BOT_TOKEN)

    @property
    def elevenlabs_enabled(self) -> bool:
        return enabled(self.ELEVENLABS_API_KEY)

    @property
    def web_api_enabled(self) -> bool:
        return enabled(self.WEB_API_BASE_URL) and enabled(self.SUPPORT_API_TOKEN)

    @property
    def mcp_enabled(self) -> bool:
        return enabled(self.MCP_SECRET)


@lru_cache
def get_settings() -> Settings:
    return Settings()
