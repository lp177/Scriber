"""Configuration management for Scriber.

Settings are resolved by layering, in increasing precedence: built-in
defaults, process environment variables, and the .env file. The module
exposes a singleton ``manager`` (created via :func:`init`) that the bot,
web and AI components share; :func:`get` returns the current snapshot.

Summary providers form an ordered failover list. Each provider is described
by an indexed block of keys — ``SUMMARY_PROVIDER_1`` / ``SUMMARY_API_KEY_1``
/ ``SUMMARY_MODEL_1`` / ``SUMMARY_BASE_URL_1``, ``..._2`` and so on. Providers
are tried in ascending index order until one succeeds. The legacy single
provider keys (``SUMMARY_PROVIDER`` without an index, etc.) are still honored
as provider 1 for backwards compatibility.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import re
import secrets
import shutil
import tempfile
from urllib.parse import urlparse

from dotenv import dotenv_values, set_key

log = logging.getLogger(__name__)

# Fixed (non-provider) keys with their defaults. Insertion order = display order.
FIXED_DEFAULTS: dict[str, str] = {
    "DISCORD_TOKEN": "",
    "DISCORD_GUILD_ID": "",
    "WHISPER_MODEL": "base",
    "WHISPER_LANGUAGE": "auto",
    "WHISPER_DEVICE": "cpu",
    "WHISPER_COMPUTE_TYPE": "int8",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "change-me",
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": "8080",
    "WEB_SECRET": "",
    "SCRIBER_DATA_DIR": "./data",
}

# Fixed keys the admin dashboard may change.
FIXED_EDITABLE_KEYS: set[str] = {
    "WHISPER_MODEL",
    "WHISPER_LANGUAGE",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
}

# Fixed keys whose values must never be displayed in clear text.
FIXED_SECRET_KEYS: set[str] = {
    "DISCORD_TOKEN",
    "ADMIN_PASSWORD",
    "WEB_SECRET",
}

# The four suffixes that make up one summary provider block.
PROVIDER_ATTRS: tuple[str, ...] = ("PROVIDER", "API_KEY", "MODEL", "BASE_URL")
# Matches an indexed provider key, e.g. ``SUMMARY_BASE_URL_2``.
_PROVIDER_KEY_RE = re.compile(r"^SUMMARY_(PROVIDER|API_KEY|MODEL|BASE_URL)_(\d+)$")
# Legacy single-provider keys (pre-list format); still honored as provider 1.
_LEGACY_KEYS: set[str] = {
    "SUMMARY_PROVIDER",
    "SUMMARY_API_KEY",
    "SUMMARY_MODEL",
    "SUMMARY_BASE_URL",
}
# Provider used when nothing at all is configured, so the app stays well-defined.
_DEFAULT_PROVIDER: dict[str, str] = {
    "PROVIDER": "anthropic",
    "API_KEY": "",
    "MODEL": "claude-opus-4-8",
    "BASE_URL": "https://api.anthropic.com",
}

_MASK = "********"


def is_editable(key: str) -> bool:
    """Return True if the admin dashboard is allowed to change this key."""
    return (
        key in FIXED_EDITABLE_KEYS
        or key in _LEGACY_KEYS
        or _PROVIDER_KEY_RE.match(key) is not None
    )


def is_secret(key: str) -> bool:
    """Return True if the key's value must never be displayed in clear text."""
    if key in FIXED_SECRET_KEYS or key == "SUMMARY_API_KEY":
        return True
    match = _PROVIDER_KEY_RE.match(key)
    return match is not None and match.group(1) == "API_KEY"


@dataclasses.dataclass
class ProviderConfig:
    """One summary provider in the ordered failover chain."""

    index: int
    provider: str
    api_key: str
    model: str
    base_url: str

    @property
    def enabled(self) -> bool:
        """A provider is tried only when it names a provider kind."""
        return bool(self.provider)

    def target_label(self) -> str:
        """Human-readable target, e.g. ``api.anthropic.com / claude-opus-4-8``."""
        netloc = urlparse(self.base_url).netloc or self.base_url or "?"
        return f"{netloc} / {self.model or '?'}"


@dataclasses.dataclass
class Config:
    """Immutable snapshot of the resolved configuration."""

    discord_token: str
    discord_guild_id: str
    summary_providers: list[ProviderConfig]
    whisper_model: str
    whisper_language: str
    whisper_device: str
    whisper_compute_type: str
    admin_username: str
    admin_password: str
    web_host: str
    web_port: int
    web_secret: str
    data_dir: pathlib.Path


class ConfigManager:
    """Loads, updates and persists Scriber configuration backed by a .env file."""

    def __init__(self, env_file: str | None = None) -> None:
        """Resolve the .env file path (``SCRIBER_ENV_FILE``, default ``./.env``) and load."""
        self.env_file: str = env_file or os.environ.get("SCRIBER_ENV_FILE") or "./.env"
        self._fixed: dict[str, str] = {}
        self._config: Config = self.reload()

    @property
    def config(self) -> Config:
        """Return the most recently loaded configuration snapshot."""
        return self._config

    def _collect_sources(self) -> dict[str, str]:
        """Merge env vars then .env file for the fixed and every ``SUMMARY_*`` key.

        .env file values take precedence over process environment variables.
        ``os.environ`` is never mutated.
        """
        sources: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in FIXED_DEFAULTS or key.startswith("SUMMARY_"):
                sources[key] = value
        for key, value in dotenv_values(self.env_file).items():
            if value is not None and (key in FIXED_DEFAULTS or key.startswith("SUMMARY_")):
                sources[key] = value
        return sources

    @staticmethod
    def _parse_providers(sources: dict[str, str]) -> list[ProviderConfig]:
        """Build the ordered provider list from the collected source keys."""
        indices = sorted(
            {int(match.group(2)) for key in sources if (match := _PROVIDER_KEY_RE.match(key))}
        )
        providers: list[ProviderConfig] = [
            ProviderConfig(
                index=n,
                provider=sources.get(f"SUMMARY_PROVIDER_{n}", "").strip(),
                api_key=sources.get(f"SUMMARY_API_KEY_{n}", ""),
                model=sources.get(f"SUMMARY_MODEL_{n}", "").strip(),
                base_url=sources.get(f"SUMMARY_BASE_URL_{n}", "").strip(),
            )
            for n in indices
        ]
        if not providers and any(key in sources for key in _LEGACY_KEYS):
            providers.append(
                ProviderConfig(
                    index=1,
                    provider=sources.get("SUMMARY_PROVIDER", "").strip(),
                    api_key=sources.get("SUMMARY_API_KEY", ""),
                    model=sources.get("SUMMARY_MODEL", "").strip(),
                    base_url=sources.get("SUMMARY_BASE_URL", "").strip(),
                )
            )
        if not providers:
            providers.append(
                ProviderConfig(
                    index=1,
                    provider=_DEFAULT_PROVIDER["PROVIDER"],
                    api_key=_DEFAULT_PROVIDER["API_KEY"],
                    model=_DEFAULT_PROVIDER["MODEL"],
                    base_url=_DEFAULT_PROVIDER["BASE_URL"],
                )
            )
        return providers

    def reload(self) -> Config:
        """Re-read all sources and rebuild the configuration snapshot.

        An empty WEB_SECRET is auto-generated and persisted to the .env file.
        """
        sources = self._collect_sources()
        fixed = {key: sources.get(key, default) for key, default in FIXED_DEFAULTS.items()}

        if not fixed["WEB_SECRET"]:
            generated = secrets.token_hex(32)
            self._persist("WEB_SECRET", generated)
            fixed["WEB_SECRET"] = generated

        try:
            web_port = int(fixed["WEB_PORT"])
        except ValueError as exc:
            raise ValueError(
                f"WEB_PORT must be an integer, got {fixed['WEB_PORT']!r}"
            ) from exc

        self._fixed = fixed
        self._config = Config(
            discord_token=fixed["DISCORD_TOKEN"],
            discord_guild_id=fixed["DISCORD_GUILD_ID"],
            summary_providers=self._parse_providers(sources),
            whisper_model=fixed["WHISPER_MODEL"],
            whisper_language=fixed["WHISPER_LANGUAGE"],
            whisper_device=fixed["WHISPER_DEVICE"],
            whisper_compute_type=fixed["WHISPER_COMPUTE_TYPE"],
            admin_username=fixed["ADMIN_USERNAME"],
            admin_password=fixed["ADMIN_PASSWORD"],
            web_host=fixed["WEB_HOST"],
            web_port=web_port,
            web_secret=fixed["WEB_SECRET"],
            data_dir=pathlib.Path(fixed["SCRIBER_DATA_DIR"]),
        )
        return self._config

    def update(self, changes: dict[str, str]) -> Config:
        """Persist editable keys to the .env file and reload.

        Accepts the fixed editable keys plus any indexed provider key
        (``SUMMARY_PROVIDER_3`` and friends), which is how the dashboard adds
        a new provider to the failover list. Raises ValueError on any other key.
        """
        invalid = {key for key in changes if not is_editable(key)}
        if invalid:
            raise ValueError(
                "Non-editable configuration key(s): " + ", ".join(sorted(invalid))
            )
        for key, value in changes.items():
            self._persist(key, str(value))
        return self.reload()

    def _display_value(self, key: str, raw: str) -> dict:
        """Build one display-field entry, masking secret values."""
        secret = is_secret(key)
        value = ("" if not raw else _MASK) if secret else raw
        return {"key": key, "value": value, "editable": is_editable(key), "secret": secret}

    def display_fields(self) -> list[dict]:
        """Return all keys for display, masking secret values.

        Order: Discord keys, each provider block (by index), then Whisper,
        admin, web and storage keys.
        """
        fields: list[dict] = [
            self._display_value("DISCORD_TOKEN", self._fixed["DISCORD_TOKEN"]),
            self._display_value("DISCORD_GUILD_ID", self._fixed["DISCORD_GUILD_ID"]),
        ]
        for provider in self._config.summary_providers:
            fields.append(
                self._display_value(f"SUMMARY_PROVIDER_{provider.index}", provider.provider)
            )
            fields.append(
                self._display_value(f"SUMMARY_API_KEY_{provider.index}", provider.api_key)
            )
            fields.append(
                self._display_value(f"SUMMARY_MODEL_{provider.index}", provider.model)
            )
            fields.append(
                self._display_value(f"SUMMARY_BASE_URL_{provider.index}", provider.base_url)
            )
        for key in (
            "WHISPER_MODEL",
            "WHISPER_LANGUAGE",
            "WHISPER_DEVICE",
            "WHISPER_COMPUTE_TYPE",
            "ADMIN_USERNAME",
            "ADMIN_PASSWORD",
            "WEB_HOST",
            "WEB_PORT",
            "WEB_SECRET",
            "SCRIBER_DATA_DIR",
        ):
            fields.append(self._display_value(key, self._fixed[key]))
        return fields

    def _persist(self, key: str, value: str) -> None:
        """Write a single key to the .env file, creating the file if missing.

        ``set_key`` writes a temp file and atomically renames it over the
        target. That rename fails with ``EBUSY`` when the .env file is a
        bind-mounted single file (e.g. ``-v ./.env:/app/.env``), because you
        cannot rename over a mount point. Fall back to rewriting the file in
        place, which keeps the mount's inode intact.
        """
        path = pathlib.Path(self.env_file)
        if path.parent != pathlib.Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        try:
            set_key(str(path), key, value)
        except OSError:
            log.warning(
                "Atomic write to %s failed (bind-mounted file?); "
                "rewriting it in place instead.",
                path,
            )
            self._persist_in_place(path, key, value)

    @staticmethod
    def _persist_in_place(path: pathlib.Path, key: str, value: str) -> None:
        """Update ``key`` in ``path`` without replacing the file's inode.

        Lets ``set_key`` build the new contents in a scratch copy (reusing its
        parsing and quoting), then overwrites the original file's bytes in place
        so a bind-mounted target is updated rather than renamed over.
        """
        with tempfile.TemporaryDirectory() as tmp:
            scratch = pathlib.Path(tmp) / "env"
            shutil.copyfile(path, scratch)
            set_key(str(scratch), key, value)
            shutil.copyfile(scratch, path)


# Module-level singleton shared by all components.
manager: ConfigManager | None = None


def init(env_file: str | None = None) -> ConfigManager:
    """Create the singleton ConfigManager and return it."""
    global manager
    manager = ConfigManager(env_file)
    return manager


def get() -> Config:
    """Return the current configuration snapshot from the singleton manager."""
    if manager is None:
        raise RuntimeError("Configuration is not initialized; call config.init() first.")
    return manager.config
