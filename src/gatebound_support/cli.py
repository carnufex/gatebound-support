"""Console script entry point: ``gatebound-support serve`` and ``gatebound-support kb-sync``."""

from __future__ import annotations

import sys


def _serve(argv: list[str]) -> int:
    import uvicorn

    from .settings import get_settings

    settings = get_settings()
    uvicorn.run("gatebound_support.app:create_app", factory=True, host="0.0.0.0", port=settings.PORT)
    return 0


def _kb_sync(argv: list[str]) -> int:
    from .kb_sync import main as kb_sync_main

    return kb_sync_main(argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] not in ("serve", "kb-sync"):
        print("usage: gatebound-support {serve|kb-sync} ...", file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "serve":
        return _serve(rest)
    return _kb_sync(rest)


if __name__ == "__main__":
    raise SystemExit(main())
