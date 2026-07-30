#!/usr/bin/env python3
"""Load a mounted KEY=VALUE secret file without persisting values in Docker metadata."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_SECRET_FILE_BYTES = 1024 * 1024


def load_env(path: Path) -> dict[str, str]:
    if path.stat().st_size > _MAX_SECRET_FILE_BYTES:
        raise ValueError("secret environment file is unexpectedly large")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError("malformed secret environment entry")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError("invalid secret environment key")
        if "\x00" in value:
            raise ValueError("NUL byte in secret environment value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit("usage: matrix_secret_entrypoint.py COMMAND [ARG ...]")
    secret_path = os.environ.pop("HERMES_ENV_FILE", "")
    homeserver_override = os.environ.pop("HERMES_MATRIX_HOMESERVER_OVERRIDE", "")
    if secret_path:
        os.environ.update(load_env(Path(secret_path)))
    if homeserver_override:
        os.environ["MATRIX_HOMESERVER"] = homeserver_override
    os.execvp(argv[1], argv[1:])


if __name__ == "__main__":
    main(sys.argv)
