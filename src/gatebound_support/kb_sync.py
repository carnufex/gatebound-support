"""``gatebound-support kb-sync`` (SPEC §7): syncs a directory of markdown files into an
ElevenLabs agent's knowledge base.

Endpoints verified against https://elevenlabs.io/docs/api-reference (fetched 2026-09-13);
see the module docstring in elevenlabs.py for the full list and paths. Auth header
``xi-api-key``.

Algorithm: new name on disk -> create text document, attach to the agent. Changed content
hash -> PATCH content in place (same id). Name missing on disk but present in the manifest ->
detach from the agent, delete the document. Finally PATCH the agent's
``conversation_config.agent.prompt.knowledge_base`` list — only entries with ``type: "text"``
that this tool created are touched; every other entry (files, URLs, folders, or text entries
this tool never created) is left exactly as found. Every created/updated document is then
queued for RAG re-indexing. The manifest is the source of truth for "does this tool manage
this document" — it is written last, after every remote call has succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Literal, Protocol

Action = Literal["create", "update", "delete", "noop"]


@dataclasses.dataclass
class PlanItem:
    name: str
    action: Action
    file: str | None = None
    doc_id: str | None = None
    body: str | None = None
    sha256: str | None = None


class KnowledgeBaseClient(Protocol):
    """The subset of ElevenLabsClient that kb-sync needs — a Protocol so tests can pass a
    fake implementation without touching the network."""

    async def create_text_document(self, *, name: str, text: str) -> dict[str, Any]: ...
    async def update_document_content(self, *, document_id: str, content: str) -> dict[str, Any]: ...
    async def delete_document(self, *, document_id: str, force: bool = True) -> None: ...
    async def request_rag_index(self, *, document_id: str, model: str = "e5_mistral_7b_instruct") -> dict[str, Any]: ...
    async def get_agent(self, agent_id: str) -> dict[str, Any]: ...
    async def update_agent_knowledge_base(self, *, agent_id: str, knowledge_base: list[dict[str, Any]]) -> None: ...
    async def get_document(self, document_id: str) -> dict[str, Any] | None: ...


def parse_markdown(path: Path) -> tuple[str, str]:
    """Returns (name, body). ``name`` comes from an optional ``name:`` front-matter field,
    defaulting to the file name without extension. The content hash is sha256 of the body
    *after* the front matter."""
    text = path.read_text(encoding="utf-8")
    default_name = path.stem
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = lines[1:].index("---") + 1
        except ValueError:
            end = None
        if end is not None:
            front = lines[1:end]
            body = "\n".join(lines[end + 1 :]).strip("\n") + "\n" if lines[end + 1 :] else ""
            name = default_name
            for line in front:
                key, _, value = line.partition(":")
                if key.strip() == "name" and value.strip():
                    name = value.strip().strip('"').strip("'")
            return name, body
    return default_name, text


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"agent_id": "", "documents": {}}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("documents", {})
    return data


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def scan_documents(directory: Path) -> dict[str, tuple[str, str, str]]:
    """Returns {name: (relative_file, body, sha256)} for every *.md file in directory."""
    result: dict[str, tuple[str, str, str]] = {}
    for path in sorted(directory.glob("*.md")):
        name, body = parse_markdown(path)
        result[name] = (path.name, body, content_hash(body))
    return result


def compute_plan(directory: Path, manifest: dict[str, Any]) -> list[PlanItem]:
    disk = scan_documents(directory)
    known = manifest.get("documents", {})
    plan: list[PlanItem] = []

    for name, (file_name, body, sha) in disk.items():
        existing = known.get(name)
        if existing is None:
            plan.append(PlanItem(name=name, action="create", file=file_name, body=body, sha256=sha))
        elif existing.get("sha256") != sha:
            plan.append(PlanItem(name=name, action="update", file=file_name, doc_id=existing.get("id"), body=body, sha256=sha))
        else:
            plan.append(PlanItem(name=name, action="noop", file=file_name, doc_id=existing.get("id"), body=body, sha256=sha))

    for name, existing in known.items():
        if name not in disk:
            plan.append(PlanItem(name=name, action="delete", doc_id=existing.get("id"), file=existing.get("file")))

    return plan


def print_plan_table(plan: list[PlanItem]) -> None:
    if not plan:
        print("(nothing to do)")
        return
    width = max(len(item.name) for item in plan)
    for item in sorted(plan, key=lambda i: (i.action, i.name)):
        print(f"{item.action:<8} {item.name:<{width}}  {item.file or ''}")


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


async def apply_plan(
    client: KnowledgeBaseClient,
    *,
    agent_id: str,
    manifest: dict[str, Any],
    plan: list[PlanItem],
) -> list[str]:
    """Applies create/update/delete, patches the agent's knowledge base, and mutates
    ``manifest`` in place. Returns the list of document ids that need RAG re-indexing."""
    documents: dict[str, Any] = manifest.setdefault("documents", {})
    manifest["agent_id"] = agent_id
    needs_index: list[str] = []
    previously_managed_ids = {entry["id"] for entry in documents.values() if entry.get("id")}

    for item in plan:
        if item.action == "create":
            created = await client.create_text_document(name=item.name, text=item.body or "")
            doc_id = created["id"]
            documents[item.name] = {
                "id": doc_id,
                "sha256": item.sha256,
                "file": item.file,
                "synced_at": _now_iso(),
            }
            needs_index.append(doc_id)
        elif item.action == "update":
            doc_id = item.doc_id or documents[item.name]["id"]
            await client.update_document_content(document_id=doc_id, content=item.body or "")
            documents[item.name] = {
                "id": doc_id,
                "sha256": item.sha256,
                "file": item.file,
                "synced_at": _now_iso(),
            }
            needs_index.append(doc_id)
        elif item.action == "delete":
            if item.doc_id:
                await client.delete_document(document_id=item.doc_id, force=True)
            documents.pop(item.name, None)
        # noop: nothing to do

    agent = await client.get_agent(agent_id)
    existing_kb: list[dict[str, Any]] = (
        agent.get("conversation_config", {}).get("agent", {}).get("prompt", {}).get("knowledge_base", []) or []
    )
    untouched = [
        entry for entry in existing_kb if not (entry.get("type") == "text" and entry.get("id") in previously_managed_ids)
    ]
    managed = [{"type": "text", "name": name, "id": entry["id"]} for name, entry in documents.items()]
    await client.update_agent_knowledge_base(agent_id=agent_id, knowledge_base=untouched + managed)

    return needs_index


async def check_remote_drift(client: KnowledgeBaseClient, manifest: dict[str, Any], plan: list[PlanItem]) -> list[str]:
    """For entries the on-disk diff considers unchanged, verify the manifest id still
    exists remotely. Returns problem descriptions (empty if none)."""
    problems = [f"{item.action}: {item.name}" for item in plan if item.action != "noop"]
    noop_ids = {item.name: item.doc_id for item in plan if item.action == "noop" and item.doc_id}
    for name, doc_id in noop_ids.items():
        if doc_id is None:
            continue
        remote = await client.get_document(doc_id)
        if remote is None:
            problems.append(f"missing_remote: {name} ({doc_id})")
    return problems


async def run_kb_sync(
    client: KnowledgeBaseClient,
    *,
    directory: Path,
    manifest_path: Path,
    agent_id: str,
    dry_run: bool,
    check: bool,
) -> int:
    manifest = load_manifest(manifest_path)
    plan = compute_plan(directory, manifest)

    if check:
        problems = await check_remote_drift(client, manifest, plan)
        if problems:
            print("kb-sync --check found differences:")
            for problem in problems:
                print(f"  {problem}")
            return 1
        print("kb-sync --check: manifest matches disk and workspace.")
        return 0

    print_plan_table(plan)
    if dry_run:
        return 0

    needs_index = await apply_plan(client, agent_id=agent_id, manifest=manifest, plan=plan)
    for doc_id in needs_index:
        await client.request_rag_index(document_id=doc_id)
    write_manifest(manifest_path, manifest)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gatebound-support kb-sync")
    parser.add_argument("--dir", required=True, type=Path, help="Directory of markdown knowledge-base files")
    parser.add_argument("--manifest", required=True, type=Path, help="Path to kb-manifest.json")
    parser.add_argument("--agent", required=True, help="ElevenLabs agent id")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan, write nothing")
    parser.add_argument("--check", action="store_true", help="Exit 1 if disk/manifest/workspace disagree, write nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    from .elevenlabs import ElevenLabsClient
    from .settings import get_settings

    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    if not settings.elevenlabs_enabled:
        print("ELEVENLABS_API_KEY is not set; kb-sync cannot run.", file=sys.stderr)
        return 2

    client = ElevenLabsClient(settings)

    async def _run() -> int:
        try:
            return await run_kb_sync(
                client,
                directory=args.dir,
                manifest_path=args.manifest,
                agent_id=args.agent,
                dry_run=args.dry_run,
                check=args.check,
            )
        finally:
            await client.aclose()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
