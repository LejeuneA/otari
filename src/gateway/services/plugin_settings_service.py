"""Dashboard-editable plugin settings, persisted in ``runtime_settings``.

A plugin's manifest types its settings; the operator's ``config.yml`` block and
these rows are the two sources of their values, rows winning, the same layering
as the gateway's own runtime overrides. Rows are keyed ``plugin:<name>:<key>``
and hold the value as JSON, so the table needs no schema change per plugin.
The runtime settings loader skips keys it does not know, which is what keeps
these rows out of its way. A setting the manifest marks ``secret`` is encrypted
with the secret box before it is written, like every other stored credential.
"""

import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.log_config import logger
from gateway.models.entities import RuntimeSetting
from gateway.models.plugins import PluginManifest, setting_value_matches
from gateway.plugins.registry import PluginRegistry
from gateway.services.secret_box import SecretDecryptionError, decrypt_secret, encrypt_secret

PREFIX = "plugin:"
# The shape a secret row holds, so a plain JSON value and a ciphertext never read alike.
ENCRYPTED_KEY = "encrypted"


def _row_key(plugin: str, key: str) -> str:
    return f"{PREFIX}{plugin}:{key}"


class PluginSettingsError(ValueError):
    """A value the manifest does not allow."""


def validate_plugin_settings(manifest: PluginManifest, values: dict[str, Any]) -> dict[str, Any]:
    """Check a dashboard write against the manifest: known, editable, and of the declared type.

    ``None`` clears a key back to its config or default value.
    """
    checked: dict[str, Any] = {}
    for key, value in values.items():
        spec = manifest.settings.get(key)
        if spec is None:
            msg = f"{key!r} is not a setting of plugin {manifest.name!r}"
            raise PluginSettingsError(msg)
        if not spec.editable:
            msg = f"{key!r} can only be set in config.yml"
            raise PluginSettingsError(msg)
        if value is not None and not setting_value_matches(spec.type, value):
            msg = f"{key!r} must be of type {spec.type}"
            raise PluginSettingsError(msg)
        checked[key] = value
    return checked


async def load_plugin_settings(session: AsyncSession, plugin: str, manifest: PluginManifest) -> dict[str, Any]:
    """The stored overrides for one plugin, decoded and decrypted."""
    prefix = _row_key(plugin, "")
    rows = (await session.execute(select(RuntimeSetting).where(RuntimeSetting.key.like(f"{prefix}%")))).scalars()
    values: dict[str, Any] = {}
    for row in rows:
        key = row.key.removeprefix(prefix)
        try:
            stored = json.loads(row.value)
        except ValueError:
            logger.warning("Plugin setting %s holds a value that is not JSON; skipped", row.key)
            continue
        spec = manifest.settings.get(key)
        if spec is not None and spec.secret:
            if not isinstance(stored, dict) or ENCRYPTED_KEY not in stored:
                logger.warning("Plugin setting %s is a secret stored in the clear; skipped", row.key)
                continue
            try:
                stored = decrypt_secret(str(stored[ENCRYPTED_KEY]))
            except (SecretDecryptionError, RuntimeError):
                logger.warning("Plugin setting %s could not be decrypted with the configured keys; skipped", row.key)
                continue
        values[key] = stored
    return values


async def save_plugin_settings(
    session: AsyncSession, plugin: str, values: dict[str, Any], manifest: PluginManifest
) -> None:
    """Persist ``values`` (already validated); a ``None`` value deletes its row. Commits.

    Raises:
        SecretBoxUnavailableError: When a secret is written and no ``OTARI_SECRET_KEY`` is set.

    """
    for key, value in values.items():
        row_key = _row_key(plugin, key)
        if value is None:
            await session.execute(delete(RuntimeSetting).where(RuntimeSetting.key == row_key))
            continue
        row = await session.get(RuntimeSetting, row_key)
        spec = manifest.settings.get(key)
        # Encrypted before it is staged, so a missing key refuses the write
        # rather than persisting a credential in the clear.
        encoded = json.dumps({ENCRYPTED_KEY: encrypt_secret(str(value))} if spec and spec.secret else value)
        if row is None:
            session.add(RuntimeSetting(key=row_key, value=encoded))
        else:
            row.value = encoded
    await session.commit()


async def apply_plugin_settings_from_db(session: AsyncSession, registry: PluginRegistry) -> None:
    """At startup, lay each plugin's stored overrides over its live config."""
    for plugin in registry.loaded():
        if not plugin.manifest.settings:
            continue
        stored = await load_plugin_settings(session, plugin.name, plugin.manifest)
        if not stored:
            continue
        values: dict[str, Any] = {}
        for key, value in stored.items():
            # Per key, so one row a newer manifest no longer accepts does not
            # take the others with it.
            try:
                values.update(validate_plugin_settings(plugin.manifest, {key: value}))
            except PluginSettingsError as error:
                logger.warning("Plugin %s: stored setting skipped: %s", plugin.name, error)
        if not values:
            continue
        await registry.apply_settings(plugin.name, {k: v for k, v in values.items() if v is not None})
        logger.info("Plugin %s: applied %d stored setting(s)", plugin.name, len(values))


def effective_values(plugin_config: dict[str, Any], manifest: PluginManifest) -> dict[str, Any]:
    """What the dashboard shows: every declared setting's live value, secrets masked to presence."""
    shown: dict[str, Any] = {}
    for key, spec in manifest.settings.items():
        value = plugin_config.get(key)
        if spec.secret:
            shown[key] = "********" if value not in (None, "") else None
        else:
            shown[key] = value
    return shown
