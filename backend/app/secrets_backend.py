"""
Secret storage abstraction: Azure Key Vault when deployed, plaintext
fallback for local development.

This is what closes the gap flagged repeatedly earlier - a Graph app's
client secret sitting in plaintext in the database is real, standing risk
independent of whether any given test succeeds or fails. With
AZURE_KEY_VAULT_URL set, the actual secret value never touches the
database at all - only the Key Vault secret's *name* does, which is
useless to anyone without separate access to the vault itself.

Falls back to storing the raw value when no Key Vault is configured, so
local development and the tests in this repo don't need real Azure
credentials - but that fallback is exactly that, a fallback, not the
intended production path.
"""
import os
from functools import lru_cache

_KEY_VAULT_URL = os.environ.get("AZURE_KEY_VAULT_URL")


@lru_cache
def _get_secret_client():
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    return SecretClient(vault_url=_KEY_VAULT_URL, credential=DefaultAzureCredential())


def is_key_vault_configured() -> bool:
    return bool(_KEY_VAULT_URL)


def store_secret(name: str, value: str) -> str:
    """Stores a secret value, returning whatever should be persisted in the
    database in its place. With Key Vault configured: the value is written
    to Key Vault and only its *name* (safe to store) is returned. Without:
    returns the raw value unchanged - dev-mode fallback, plaintext in the
    database, same as before.
    """
    if not _KEY_VAULT_URL:
        return value
    client = _get_secret_client()
    # Key Vault secret names allow only alphanumerics and hyphens.
    safe_name = name.replace("_", "-")
    client.set_secret(safe_name, value)
    return safe_name


def resolve_secret(stored_value: str) -> str:
    """Reverses store_secret: given what's in the database, returns the
    real value the caller actually needs to use.
    """
    if not _KEY_VAULT_URL:
        return stored_value
    client = _get_secret_client()
    return client.get_secret(stored_value).value
