"""Talks to the deployed MCP server the way ElevenLabs does: initialize, tools/list, and a
few tools/call round-trips over Streamable HTTP with the bearer secret.

    MCP_SECRET=... uv run python elevenlabs/scripts/probe_mcp.py [base_url]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "https://gatebound-support.rosenvall.se").rstrip("/")
    secret = os.environ.get("MCP_SECRET")
    if not secret:
        print("MCP_SECRET is not set", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {secret}", "X-Conversation-Id": "probe"}
    async with (
        streamablehttp_client(f"{base}/mcp", headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        print("tools:", [t.name for t in tools.tools])
        for name, args in [
            ("get_server_status", {}),
            ("lookup_character", {"name": "nobody_xyz"}),
            ("get_monster", {"name": "dragon"}),
            ("search_library", {"query": "dragon"}),
            ("get_my_account", {}),
            ("create_ticket_link", {"category": "other", "summary": "probe ticket from probe_mcp.py", "priority": "normal"}),
        ]:
            result = await session.call_tool(name, args)
            text = result.content[0].text if result.content else ""
            try:
                data = json.loads(text)
                shown = {k: data[k] for k in ("status", "spoken_summary", "url") if k in data}
            except json.JSONDecodeError:
                shown = text[:200]
            print(f"{name}: {json.dumps(shown)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
