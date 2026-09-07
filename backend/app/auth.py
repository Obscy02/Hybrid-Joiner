"""
Four auth levels, deliberately kept separate even in a single-tenant
deployment:

  - Root token (DASHBOARD_TOKEN) -> a single break-glass credential that
    can do anything, including issue the first admin key. Meant for
    bootstrap and recovery, not day-to-day use once named admin keys exist.
  - Admin keys -> named, individually revocable operator access: editing
    Config, storing the Graph credential, issuing/revoking keys. Letting
    more than one person have their own credential (instead of everyone
    sharing DASHBOARD_TOKEN) means revoking one person's access doesn't
    require rotating a secret everyone else also uses, and the audit log
    can say which person did something.
  - Client keys -> the admin-team GUI/script. Can submit and read jobs,
    nothing else.
  - Connector keys -> the on-prem agent. Can claim/execute jobs and read
    Config, nothing else.
"""
from __future__ import annotations

import hashlib
import os
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session, select

from .db import get_session
from .models import ApiKey, KeyRole

# Read from the environment in any real deployment (set DASHBOARD_TOKEN) -
# the literal fallback only exists so local/dev runs work with zero setup.
# Never deploy this to somewhere reachable from the internet without
# setting a real one; whoever holds it can do anything, including issue
# more admin keys.
DEV_DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "dev-only-change-me")


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _lookup_key(role: KeyRole, token: str, session: Session) -> ApiKey | None:
    key_hash = hash_key(token)
    return session.exec(
        select(ApiKey).where(
            ApiKey.key_hash == key_hash,
            ApiKey.role == role,
            ApiKey.revoked == False,  # noqa: E712
        )
    ).first()


def require_admin(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> str:
    """Accepts either the root DASHBOARD_TOKEN or a valid, non-revoked
    admin-role key. Returns an actor label for the audit log - "root" or
    "admin:<label>" - so a shared root token doesn't erase who actually
    did something once named admin keys are in use.
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing credentials")

    if secrets.compare_digest(token, DEV_DASHBOARD_TOKEN):
        return "root"

    key_row = _lookup_key(KeyRole.admin, token, session)
    if key_row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return f"admin:{key_row.label}"


def _require_key(role: KeyRole, authorization: str, session: Session) -> str:
    """Validates the bearer token against issued keys of the given role.
    Returns the key's label (for audit logging - "connector:<label>" is
    more useful in the trail than just "connector").
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Missing {role.value} key")

    key_row = _lookup_key(role, token, session)
    if key_row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid or revoked {role.value} key")
    return key_row.label


def require_connector(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> str:
    return _require_key(KeyRole.connector, authorization, session)


def require_client(
    authorization: str = Header(default=""),
    session: Session = Depends(get_session),
) -> str:
    return _require_key(KeyRole.client, authorization, session)
