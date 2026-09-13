"""The DAVE receive-side hook (voicebot/dave.py): payload decryption per packet, dropping
undecryptable packets, and swallowing OpusError so the router thread survives."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from discord.ext.voice_recv import opus as vr_opus
from discord.ext.voice_recv import router as vr_router
from discord.opus import OpusError

from gatebound_support.voicebot import dave


class FakeSession:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[int, bytes]] = []

    def decrypt(self, user_id: int, media_type, packet: bytes) -> bytes:
        self.calls.append((user_id, packet))
        if self.fail:
            raise RuntimeError("bad frame")
        return b"plain:" + packet


def make_voice_client(session=None, ready: bool = True, ssrc_map: dict[int, int] | None = None):
    connection = SimpleNamespace(dave_session=session, can_encrypt=ready)
    vc = SimpleNamespace(_connection=connection)
    vc._get_id_from_ssrc = lambda ssrc: (ssrc_map or {}).get(ssrc)
    return vc


def test_payload_passes_through_without_ready_session():
    vc = make_voice_client(session=None, ready=False)
    assert dave.decrypt_payload(vc, 42, b"opus") == b"opus"
    vc = make_voice_client(session=FakeSession(), ready=False)
    assert dave.decrypt_payload(vc, 42, b"opus") == b"opus"


def test_payload_is_decrypted_for_known_sender():
    session = FakeSession()
    vc = make_voice_client(session=session, ready=True, ssrc_map={42: 1234})
    assert dave.decrypt_payload(vc, 42, b"cipher") == b"plain:cipher"
    assert session.calls == [(1234, b"cipher")]


def test_unknown_sender_or_failed_decrypt_drops_packet():
    vc = make_voice_client(session=FakeSession(), ready=True, ssrc_map={})
    assert dave.decrypt_payload(vc, 42, b"cipher") is None
    vc = make_voice_client(session=FakeSession(fail=True), ready=True, ssrc_map={42: 1})
    assert dave.decrypt_payload(vc, 42, b"cipher") is None


def test_install_patches_router_and_decoder(monkeypatch):
    monkeypatch.setattr(vr_router.PacketRouter, "_gb_dave_patched", False, raising=False)
    fed: list[bytes] = []
    monkeypatch.setattr(vr_router.PacketRouter, "feed_rtp", lambda self, packet: fed.append(packet.decrypted_data))

    def boom(self, packet):
        # OpusError.__init__ calls into libopus for the message; build it without libopus.
        err = OpusError.__new__(OpusError)
        err.code = -4
        raise err

    monkeypatch.setattr(vr_opus.PacketDecoder, "_decode_packet", boom)

    dave.install_dave_receive()
    assert vr_router.PacketRouter._gb_dave_patched is True

    session = FakeSession()
    vc = make_voice_client(session=session, ready=True, ssrc_map={7: 99})
    router = SimpleNamespace(reader=SimpleNamespace(voice_client=vc))
    packet = SimpleNamespace(ssrc=7, decrypted_data=b"cipher")
    vr_router.PacketRouter.feed_rtp(router, packet)
    assert fed == [b"plain:cipher"]

    unknown = SimpleNamespace(ssrc=8, decrypted_data=b"cipher")
    vr_router.PacketRouter.feed_rtp(router, unknown)
    assert fed == [b"plain:cipher"]  # dropped, not fed

    decoder = SimpleNamespace()
    result_packet, pcm = vr_opus.PacketDecoder._decode_packet(decoder, packet)
    assert result_packet is packet and pcm == dave.SILENCE_PCM


@pytest.mark.parametrize("attr", ["feed_rtp", "_gb_dave_patched"])
def test_install_is_idempotent(monkeypatch, attr):
    monkeypatch.setattr(vr_router.PacketRouter, "_gb_dave_patched", True, raising=False)
    before = getattr(vr_router.PacketRouter, attr)
    dave.install_dave_receive()
    assert getattr(vr_router.PacketRouter, attr) is before
