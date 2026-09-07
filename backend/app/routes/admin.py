"""
Operator-facing routes for configuring this deployment. This is the
generic replacement for hand-editing Config.ps1 - the same categories of
value, but as data behind an API instead of a maintainer editing a script.

Single-tenant: Config and GraphCredential are singletons (always id=1) -
there's no tenant creation step, because this whole deployment IS the
tenant. A second customer gets an entirely separate deployment of this
same codebase, not a new row here.

Accepts either the root DASHBOARD_TOKEN or a named admin key (see
auth.require_admin) - every mutation writes an AuditLog row naming
whichever one was actually used, not a generic "dashboard" label, so more
than one person can have their own revocable access with real
accountability instead of a single shared secret.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from pydantic import ValidationError

from ..audit import log_action
from ..auth import generate_api_key, hash_key, require_admin
from ..db import get_session
from ..models import ApiKey, AuditLog, CloudGroupRule, Config, GraphCredential, KeyRole, SINGLETON_ID
from ..retention import purge_old_jobs
from ..secrets_backend import store_secret

router = APIRouter()


@router.get("/config", response_model=Config)
def get_config(_actor: str = Depends(require_admin), session: Session = Depends(get_session)):
    config = session.get(Config, SINGLETON_ID)
    if config is None:
        raise HTTPException(status_code=404, detail="Not configured yet - PUT /config first")
    return config


@router.put("/config", response_model=Config)
def put_config(updated: Config, actor: str = Depends(require_admin), session: Session = Depends(get_session)):
    """Upsert - the first call creates the singleton row, later calls
    replace it. Same endpoint either way, so a customer's initial setup
    script and a later "update the group list" call look identical.

    cloud_group_rules is stored as a raw JSON list (see models.py for why
    it can't just be typed list[CloudGroupRule] and get this for free), so
    each rule is validated explicitly here - a typo'd or incomplete rule
    (e.g. missing display_name) is rejected now, with a 422 that names the
    problem, rather than surfacing days later as a specific joiner's job
    silently skipping a group with no admin-visible warning until then.
    """
    for i, rule in enumerate(updated.cloud_group_rules):
        try:
            CloudGroupRule.model_validate(rule)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=f"cloud_group_rules[{i}] is invalid: {exc}")

    existing = session.get(Config, SINGLETON_ID)
    data = updated.model_dump(exclude={"id"})
    if existing is None:
        existing = Config(id=SINGLETON_ID, **data)
        session.add(existing)
    else:
        for field, value in data.items():
            setattr(existing, field, value)
        session.add(existing)

    session.commit()
    log_action(session, actor, "config.update")
    session.refresh(existing)
    return existing


@router.put("/graph-credential")
def set_graph_credential(
    cred: GraphCredential, actor: str = Depends(require_admin), session: Session = Depends(get_session)
):
    """Stores the Entra app registration used for the Graph-only steps.
    With AZURE_KEY_VAULT_URL configured, the actual client secret is
    written to Key Vault and only its secret *name* is persisted here - the
    database never holds the real value. Without it (local dev), falls
    back to storing the raw value directly.
    """
    cred.id = SINGLETON_ID
    cred.client_secret = store_secret("graph-client-secret", cred.client_secret)
    session.merge(cred)
    session.commit()

    # Never put the secret (or its Key Vault name) in the audit detail.
    log_action(session, actor, "graph_credential.set", detail=f"client_id={cred.client_id}")
    return {"ok": True}


@router.post("/keys")
def issue_key(label: str, role: KeyRole, actor: str = Depends(require_admin), session: Session = Depends(get_session)):
    """Issues a new API key, scoped to one role:
      - "connector" -> goes on the on-prem agent machine.
      - "client"    -> goes in the admin team's joiner-submission GUI/script.
      - "admin"     -> a named person's own operator access (see auth.py).

    The plaintext key is returned exactly once in this response - only its
    hash is stored. Losing it means issuing a new one, not recovering the
    old one.
    """
    plaintext = generate_api_key()
    key_row = ApiKey(role=role, label=label, key_hash=hash_key(plaintext))
    session.add(key_row)
    session.commit()
    session.refresh(key_row)

    log_action(session, actor, "key.issue", detail=f"role={role} label={label} key_id={key_row.id}")
    return {"api_key": plaintext, "role": role, "key_id": key_row.id, "note": "Store this now - it will not be shown again."}


@router.get("/keys")
def list_keys(_actor: str = Depends(require_admin), session: Session = Depends(get_session)):
    """Lists issued keys - metadata only (id, role, label, created_at,
    revoked), never the key value or its hash.
    """
    keys = session.exec(select(ApiKey)).all()
    return [
        {"id": k.id, "role": k.role, "label": k.label, "created_at": k.created_at, "revoked": k.revoked}
        for k in keys
    ]


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: int, actor: str = Depends(require_admin), session: Session = Depends(get_session)):
    """Revokes one key immediately - a compromised or retired admin/
    connector/client credential stops working on its very next request.
    """
    key_row = session.get(ApiKey, key_id)
    if key_row is None:
        raise HTTPException(status_code=404, detail="Key not found")

    key_row.revoked = True
    session.add(key_row)
    session.commit()

    log_action(session, actor, "key.revoke", detail=f"key_id={key_id} label={key_row.label}")
    return {"ok": True}


@router.get("/audit-log", response_model=List[AuditLog])
def get_audit_log(_actor: str = Depends(require_admin), session: Session = Depends(get_session)):
    entries = session.exec(select(AuditLog).order_by(AuditLog.created_at.desc())).all()
    return entries


@router.post("/jobs/purge")
def purge_jobs(
    older_than_days: int = 90,
    actor: str = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Deletes finished (succeeded/failed) job records older than
    older_than_days - see retention.py for why this exists and why it's
    manual rather than an automatic schedule.
    """
    if older_than_days < 1:
        raise HTTPException(status_code=400, detail="older_than_days must be at least 1")

    deleted_count = purge_old_jobs(session, older_than_days)
    log_action(session, actor, "jobs.purge", detail=f"older_than_days={older_than_days} deleted={deleted_count}")
    return {"deleted": deleted_count}
