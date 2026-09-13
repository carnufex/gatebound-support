"""Console script entry point: ``gatebound-support serve`` and ``gatebound-support kb-sync``."""

from __future__ import annotations

import sys


def _serve(argv: list[str]) -> int:
    import uvicorn

    from .settings import get_settings

    settings = get_settings()
    # Behind the cluster gateway (TLS terminated upstream): trust X-Forwarded-* so any
    # generated absolute URL or redirect keeps the https scheme.
    uvicorn.run(
        "gatebound_support.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=settings.PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def _kb_sync(argv: list[str]) -> int:
    from .kb_sync import main as kb_sync_main

    return kb_sync_main(argv)


def _voicebot(argv: list[str]) -> int:
    from .settings import get_settings
    from .voicebot.runner import check, run

    settings = get_settings()
    if "--check" in argv:
        return check(settings)
    return run(settings)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] not in ("serve", "kb-sync", "voicebot"):
        print("usage: gatebound-support {serve|kb-sync|voicebot} ...", file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "serve":
        return _serve(rest)
    if command == "voicebot":
        return _voicebot(rest)
    return _kb_sync(rest)


if __name__ == "__main__":
    raise SystemExit(main())
