from __future__ import annotations

import time

import jwt
import pytest

from gatebound_support.identity import verify_identity_token

from .conftest import make_settings

SECRET = "identity-test-secret"


@pytest.fixture
def settings(base_env):
    env = dict(base_env)
    env["SUPPORT_IDENTITY_SECRET"] = SECRET
    return make_settings(env)


def _token(**overrides) -> str:
    now = int(time.time())
    payload = {
        "iss": "gatebound-web",
        "aud": "gatebound-support",
        "sub": "12345",
        "name": "Aidenn",
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


def test_valid_token(settings) -> None:
    identity = verify_identity_token(_token(), settings)
    assert identity == {"account_id": "12345", "name": "Aidenn"}


def test_expired_token(settings) -> None:
    now = int(time.time())
    token = _token(iat=now - 7200, exp=now - 3600)
    assert verify_identity_token(token, settings) is None


def test_wrong_audience(settings) -> None:
    token = _token(aud="someone-else")
    assert verify_identity_token(token, settings) is None


def test_wrong_issuer(settings) -> None:
    token = _token(iss="not-gatebound-web")
    assert verify_identity_token(token, settings) is None


def test_no_token(settings) -> None:
    assert verify_identity_token(None, settings) is None
    assert verify_identity_token("", settings) is None


def test_disabled_when_secret_unset(base_env) -> None:
    settings = make_settings(base_env)  # SUPPORT_IDENTITY_SECRET == "unset"
    assert verify_identity_token(_token(), settings) is None


def test_wrong_signing_key(settings) -> None:
    token = jwt.encode(
        {
            "iss": "gatebound-web",
            "aud": "gatebound-support",
            "sub": "1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        "a-different-secret",
        algorithm="HS256",
    )
    assert verify_identity_token(token, settings) is None
