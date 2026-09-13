from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gatebound_support import kb_sync


class FakeElevenLabsClient:
    def __init__(self, agent_kb: list[dict[str, Any]] | None = None, existing_docs: dict[str, str] | None = None) -> None:
        self.documents: dict[str, str] = dict(existing_docs or {})
        self._next_id = 1
        self.agent_kb: list[dict[str, Any]] = list(agent_kb or [])
        self.rag_indexed: list[str] = []
        self.deleted: list[str] = []

    async def create_text_document(self, *, name: str, text: str) -> dict[str, Any]:
        doc_id = f"doc-{self._next_id}"
        self._next_id += 1
        self.documents[doc_id] = text
        return {"id": doc_id, "name": name}

    async def update_document_content(self, *, document_id: str, content: str) -> dict[str, Any]:
        self.documents[document_id] = content
        return {"id": document_id}

    async def delete_document(self, *, document_id: str, force: bool = True) -> None:
        self.documents.pop(document_id, None)
        self.deleted.append(document_id)

    async def request_rag_index(self, *, document_id: str, model: str = "e5_mistral_7b_instruct") -> dict[str, Any]:
        self.rag_indexed.append(document_id)
        return {"id": document_id, "status": "processing"}

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return {"conversation_config": {"agent": {"prompt": {"knowledge_base": self.agent_kb}}}}

    async def update_agent_knowledge_base(self, *, agent_id: str, knowledge_base: list[dict[str, Any]]) -> None:
        self.agent_kb = knowledge_base

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        if document_id in self.documents:
            return {"id": document_id}
        return None


def write_md(directory: Path, filename: str, body: str, name: str | None = None) -> Path:
    text = f"---\nname: {name}\n---\n{body}" if name else body
    path = directory / filename
    path.write_text(text, encoding="utf-8")
    return path


# ---- parse_markdown ----


def test_parse_markdown_uses_front_matter_name(tmp_path: Path) -> None:
    path = write_md(tmp_path, "rules.md", "Body text here.\n", name="Server Rules")
    name, body = kb_sync.parse_markdown(path)
    assert name == "Server Rules"
    assert body.strip() == "Body text here."


def test_parse_markdown_defaults_name_to_filename(tmp_path: Path) -> None:
    path = write_md(tmp_path, "faq.md", "Some FAQ content.\n")
    name, body = kb_sync.parse_markdown(path)
    assert name == "faq"
    assert "Some FAQ content." in body


# ---- compute_plan ----


def test_plan_create_for_new_document(tmp_path: Path) -> None:
    write_md(tmp_path, "a.md", "Content A")
    plan = kb_sync.compute_plan(tmp_path, {"documents": {}})
    assert len(plan) == 1
    assert plan[0].action == "create"
    assert plan[0].name == "a"


def test_plan_update_for_changed_hash(tmp_path: Path) -> None:
    write_md(tmp_path, "a.md", "New content")
    manifest = {"documents": {"a": {"id": "doc-1", "sha256": "stale-hash", "file": "a.md"}}}
    plan = kb_sync.compute_plan(tmp_path, manifest)
    assert len(plan) == 1
    assert plan[0].action == "update"
    assert plan[0].doc_id == "doc-1"


def test_plan_noop_for_unchanged_hash(tmp_path: Path) -> None:
    path = write_md(tmp_path, "a.md", "Same content")
    _, body = kb_sync.parse_markdown(path)
    sha = kb_sync.content_hash(body)
    manifest = {"documents": {"a": {"id": "doc-1", "sha256": sha, "file": "a.md"}}}
    plan = kb_sync.compute_plan(tmp_path, manifest)
    assert len(plan) == 1
    assert plan[0].action == "noop"


def test_plan_delete_for_manifest_entry_missing_on_disk(tmp_path: Path) -> None:
    manifest = {"documents": {"gone": {"id": "doc-9", "sha256": "x", "file": "gone.md"}}}
    plan = kb_sync.compute_plan(tmp_path, manifest)
    assert len(plan) == 1
    assert plan[0].action == "delete"
    assert plan[0].doc_id == "doc-9"


def test_plan_mixed_create_update_delete_noop(tmp_path: Path) -> None:
    write_md(tmp_path, "new.md", "brand new")
    write_md(tmp_path, "changed.md", "changed body")
    same_path = write_md(tmp_path, "same.md", "unchanged body")
    _, same_body = kb_sync.parse_markdown(same_path)
    manifest = {
        "documents": {
            "changed": {"id": "doc-changed", "sha256": "stale", "file": "changed.md"},
            "same": {"id": "doc-same", "sha256": kb_sync.content_hash(same_body), "file": "same.md"},
            "removed": {"id": "doc-removed", "sha256": "x", "file": "removed.md"},
        }
    }
    plan = kb_sync.compute_plan(tmp_path, manifest)
    actions = {item.name: item.action for item in plan}
    assert actions == {"new": "create", "changed": "update", "same": "noop", "removed": "delete"}


# ---- apply_plan / run_kb_sync ----


async def test_apply_plan_creates_document_and_updates_agent_kb(tmp_path: Path) -> None:
    write_md(tmp_path, "a.md", "Content A")
    client = FakeElevenLabsClient()
    manifest_path = tmp_path / "kb-manifest.json"
    exit_code = await kb_sync.run_kb_sync(
        client, directory=tmp_path, manifest_path=manifest_path, agent_id="agent-1", dry_run=False, check=False
    )
    assert exit_code == 0
    manifest = json.loads(manifest_path.read_text())
    assert manifest["agent_id"] == "agent-1"
    doc_id = manifest["documents"]["a"]["id"]
    assert client.documents[doc_id] == "Content A"
    assert {"type": "text", "name": "a", "id": doc_id} in client.agent_kb
    assert doc_id in client.rag_indexed


async def test_apply_plan_deletes_and_detaches_removed_document(tmp_path: Path) -> None:
    client = FakeElevenLabsClient(
        agent_kb=[{"type": "text", "name": "gone", "id": "doc-9"}],
        existing_docs={"doc-9": "old body"},
    )
    manifest_path = tmp_path / "kb-manifest.json"
    manifest_path.write_text(json.dumps({"agent_id": "agent-1", "documents": {"gone": {"id": "doc-9", "sha256": "x", "file": "gone.md"}}}))
    exit_code = await kb_sync.run_kb_sync(
        client, directory=tmp_path, manifest_path=manifest_path, agent_id="agent-1", dry_run=False, check=False
    )
    assert exit_code == 0
    manifest = json.loads(manifest_path.read_text())
    assert "gone" not in manifest["documents"]
    assert "doc-9" in client.deleted
    assert all(entry.get("id") != "doc-9" for entry in client.agent_kb)


async def test_apply_plan_preserves_untouched_kb_entries(tmp_path: Path) -> None:
    write_md(tmp_path, "a.md", "Content A")
    client = FakeElevenLabsClient(agent_kb=[{"type": "file", "name": "manual.pdf", "id": "file-1"}])
    manifest_path = tmp_path / "kb-manifest.json"
    await kb_sync.run_kb_sync(
        client, directory=tmp_path, manifest_path=manifest_path, agent_id="agent-1", dry_run=False, check=False
    )
    assert {"type": "file", "name": "manual.pdf", "id": "file-1"} in client.agent_kb


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    write_md(tmp_path, "a.md", "Content A")
    client = FakeElevenLabsClient()
    manifest_path = tmp_path / "kb-manifest.json"
    exit_code = await kb_sync.run_kb_sync(
        client, directory=tmp_path, manifest_path=manifest_path, agent_id="agent-1", dry_run=True, check=False
    )
    assert exit_code == 0
    assert not manifest_path.exists()
    assert client.documents == {}


async def test_check_exits_nonzero_on_diff(tmp_path: Path) -> None:
    write_md(tmp_path, "a.md", "Changed content")
    client = FakeElevenLabsClient()
    manifest_path = tmp_path / "kb-manifest.json"
    manifest_path.write_text(json.dumps({"agent_id": "agent-1", "documents": {"a": {"id": "doc-1", "sha256": "stale", "file": "a.md"}}}))
    exit_code = await kb_sync.run_kb_sync(
        client, directory=tmp_path, manifest_path=manifest_path, agent_id="agent-1", dry_run=False, check=True
    )
    assert exit_code == 1


async def test_check_exits_zero_when_clean(tmp_path: Path) -> None:
    path = write_md(tmp_path, "a.md", "Stable content")
    _, body = kb_sync.parse_markdown(path)
    sha = kb_sync.content_hash(body)
    client = FakeElevenLabsClient(existing_docs={"doc-1": body})
    manifest_path = tmp_path / "kb-manifest.json"
    manifest_path.write_text(json.dumps({"agent_id": "agent-1", "documents": {"a": {"id": "doc-1", "sha256": sha, "file": "a.md"}}}))
    exit_code = await kb_sync.run_kb_sync(
        client, directory=tmp_path, manifest_path=manifest_path, agent_id="agent-1", dry_run=False, check=True
    )
    assert exit_code == 0


async def test_check_exits_nonzero_when_manifest_id_missing_remotely(tmp_path: Path) -> None:
    path = write_md(tmp_path, "a.md", "Stable content")
    _, body = kb_sync.parse_markdown(path)
    sha = kb_sync.content_hash(body)
    client = FakeElevenLabsClient(existing_docs={})  # doc-1 does not exist remotely
    manifest_path = tmp_path / "kb-manifest.json"
    manifest_path.write_text(json.dumps({"agent_id": "agent-1", "documents": {"a": {"id": "doc-1", "sha256": sha, "file": "a.md"}}}))
    exit_code = await kb_sync.run_kb_sync(
        client, directory=tmp_path, manifest_path=manifest_path, agent_id="agent-1", dry_run=False, check=True
    )
    assert exit_code == 1

