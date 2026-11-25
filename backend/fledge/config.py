"""Settings from environment variables.

For local development, values can also live in the repo-root `.env` file (the
same one docker compose reads). Real environment variables always win, so
production never depends on a file. This tiny reader stands in for a dotenv
library: it only understands KEY=VALUE lines and # comments.
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class MissingSettingError(RuntimeError):
    pass


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def optional_setting(name: str, default: str = "") -> str:
    """A setting that may be blank (e.g. no SMTP username for the local Mailpit)."""
    value = os.environ.get(name)
    if value is None:
        value = _read_env_file(ENV_FILE).get(name)
    return default if value is None else value


def setting(name: str) -> str:
    value = optional_setting(name)
    if not value:
        raise MissingSettingError(
            f"{name} is not set. Copy .env.example to .env (repo root) or set it in your shell."
        )
    return value


def database_url() -> str:
    return setting("DATABASE_URL")


def test_database_url() -> str:
    return setting("TEST_DATABASE_URL")
