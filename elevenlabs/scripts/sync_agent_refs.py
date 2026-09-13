"""Copies the live agent's knowledge_base list and mcp_server_ids into the committed
agent config, so the next `elevenlabs agents push` (which force-overrides) does not wipe
what kb-sync or the MCP registration set up.

    python scripts/sync_agent_refs.py          # needs ELEVENLABS_API_KEY
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CONFIG = HERE / "agent_configs" / "Gatebound-Support.json"


def main() -> int:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        print("ELEVENLABS_API_KEY is not set", file=sys.stderr)
        return 2
    registry = json.loads((HERE / "agents.json").read_text(encoding="utf-8"))
    agent_id = registry["agents"][0].get("id")
    if not agent_id:
        print("agents.json has no agent id yet (push first)", file=sys.stderr)
        return 2
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/convai/agents/{agent_id}", headers={"xi-api-key": key}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        live = json.loads(r.read().decode())
    live_prompt = live["conversation_config"]["agent"]["prompt"]
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    prompt = cfg["conversation_config"]["agent"]["prompt"]
    prompt["knowledge_base"] = live_prompt.get("knowledge_base", [])
    prompt["mcp_server_ids"] = live_prompt.get("mcp_server_ids", [])
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"knowledge_base: {len(prompt['knowledge_base'])} document(s), mcp_server_ids: {prompt['mcp_server_ids']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
