"""
The cloud-only half of provisioning: Entra group membership + M365
licensing. Pure REST against Microsoft Graph, so unlike AD user creation
this runs directly from the backend - no on-prem connector hop needed.

Uses an app-only (client credentials) Graph app registration rather than
the interactive/device-code flows an earlier interactive desktop tool used, since there's
no human sitting at this process to click through a sign-in prompt. This
is the standard shape for a service that acts without a signed-in user -
this deployment's one Entra app registration, granted application
permissions (not delegated), configured once via PUT /graph-credential.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import update as sa_update
from sqlmodel import Session, select, or_

from .db import engine
from .models import CloudGroupRule, Config, GraphCredential, Job, JobStatus, SINGLETON_ID
from .secrets_backend import resolve_secret

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# On-prem -> Entra sync isn't instant. An earlier desktop version of this tool polled for up
# to 15 minutes (60 retries * 15s) before giving up - same budget here,
# just running as a background retry instead of blocking a human's click.
SYNC_POLL_ATTEMPTS = 30
SYNC_POLL_INTERVAL_SECONDS = 30

# How long a cloud-steps claim is honored before another attempt is
# allowed to take over - comfortably longer than the max processing time
# above (30 * 30s = 15 minutes) so a still-running attempt is never
# preempted, but short enough that a genuinely dead attempt's job doesn't
# stay stuck for long.
CLOUD_STEPS_CLAIM_STALE_AFTER = timedelta(minutes=20)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_app_token(cred: GraphCredential) -> str:
    token_url = f"https://login.microsoftonline.com/{cred.aad_tenant_id}/oauth2/v2.0/token"
    resp = httpx.post(
        token_url,
        data={
            "client_id": cred.client_id,
            "client_secret": resolve_secret(cred.client_secret),
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _rule_matches(rule: dict, joiner_fields: dict) -> bool:
    condition = rule.get("condition", "always")
    if condition == "always":
        return True
    if condition == "is_line_manager":
        return str(joiner_fields.get("is_line_manager", "")).strip().lower() == "yes"
    if condition == "has_mobile_device":
        return str(joiner_fields.get("mobile_device_requested", "")).strip().lower() == "yes"
    if condition.startswith("field:"):
        field_name = condition.split("field:", 1)[1]
        value = str(joiner_fields.get(field_name, "")).strip()
        placeholder_markers = ("complete if", "to be completed", "not required", "n/a")
        return bool(value) and not any(m in value.lower() for m in placeholder_markers)
    if condition.startswith("field_matches:") or condition.startswith("field_not_matches:"):
        negate = condition.startswith("field_not_matches:")
        _, field_name, pattern = condition.split(":", 2)
        value = str(joiner_fields.get(field_name, ""))
        matched = bool(re.search(pattern, value, re.IGNORECASE))
        return (not matched) if negate else matched
    return False


def _find_group_id(token: str, display_name: str) -> str | None:
    resp = httpx.get(
        f"{GRAPH_BASE}/groups",
        headers={"Authorization": f"Bearer {token}"},
        params={"$filter": f"displayName eq '{display_name}'"},
        timeout=30,
    )
    resp.raise_for_status()
    values = resp.json().get("value", [])
    return values[0]["id"] if values else None


def _find_user_id(token: str, upn: str) -> str | None:
    resp = httpx.get(f"{GRAPH_BASE}/users/{upn}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["id"]


def _resolve_sku_ids(token: str, sku_part_numbers: list[str]) -> list[str]:
    """License config stores human-readable SKU part numbers (e.g.
    "ENTERPRISEPACK"), but assignLicense needs the tenant's actual skuId
    GUIDs - those are only knowable by listing the tenant's own subscribed
    SKUs, same as the equivalent Get-MgSubscribedSku step in an earlier desktop version of this tool.
    """
    resp = httpx.get(f"{GRAPH_BASE}/subscribedSkus", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    resp.raise_for_status()
    by_part_number = {sku["skuPartNumber"]: sku["skuId"] for sku in resp.json().get("value", [])}
    return [by_part_number[p] for p in sku_part_numbers if p in by_part_number]


def _append_log(session: Session, job: Job, line: str) -> None:
    job.log_lines = [*job.log_lines, line]
    session.add(job)
    session.commit()
    session.refresh(job)


def find_interrupted_jobs(session: Session) -> list[int]:
    """Jobs left in awaiting_cloud_steps are ones whose run_cloud_steps
    background task was in flight - possibly mid-way through its up-to-
    15-minute Entra sync poll - when the process stopped. FastAPI
    background tasks live only in process memory; a restart (a deploy, a
    crash, routine platform maintenance) doesn't fail them, it just makes
    them vanish, silently, with the job stuck forever and no error to see.

    The state that matters (the job IS awaiting_cloud_steps) is already
    durable in Postgres, committed before the background task was ever
    scheduled - so recovery is just: on startup, find jobs in that state
    and resume them. See main.py's lifespan for where this gets called.

    Only jobs with no claim, or a claim older than
    CLOUD_STEPS_CLAIM_STALE_AFTER, count as "interrupted" - a job another
    (still-running) attempt claimed recently is presumed still in
    progress, not stuck. Combined with _try_claim_for_cloud_steps()'s
    atomic claim, this makes it safe to call find_interrupted_jobs() from
    more than one backend instance without both instances retrying the
    same job - only one will win the claim.
    """
    cutoff = _utcnow() - CLOUD_STEPS_CLAIM_STALE_AFTER
    return list(
        session.exec(
            select(Job.id).where(
                Job.status == JobStatus.awaiting_cloud_steps,
                or_(Job.cloud_steps_claimed_at == None, Job.cloud_steps_claimed_at < cutoff),  # noqa: E711
            )
        ).all()
    )


def _try_claim_for_cloud_steps(session: Session, job_id: int) -> bool:
    """Atomic optimistic claim: a single UPDATE ... WHERE ... that only
    succeeds if nobody holds a recent claim on this job. Read-then-write
    would let two processes racing on the same job both read "unclaimed"
    and both proceed; this way only one UPDATE actually matches a row.

    synchronize_session=False: SQLAlchemy's default bulk-update strategy
    re-evaluates the WHERE clause in plain Python against any matching
    object already in this session's identity map, to keep it in sync.
    That evaluation compares datetimes with Python's `<`, which raises if
    one side is naive and the other aware - and SQLite round-trips a
    timezone-aware datetime back as naive. We don't need that sync here
    (the caller re-fetches the row fresh via session.get right after), so
    skip it rather than fight the naive/aware mismatch.
    """
    cutoff = _utcnow() - CLOUD_STEPS_CLAIM_STALE_AFTER
    stmt = (
        sa_update(Job)
        .where(Job.id == job_id)
        .where(or_(Job.cloud_steps_claimed_at == None, Job.cloud_steps_claimed_at < cutoff))  # noqa: E711
        .values(cloud_steps_claimed_at=_utcnow())
        .execution_options(synchronize_session=False)
    )
    result = session.exec(stmt)
    session.commit()
    return result.rowcount == 1


def _fail(session: Session, job: Job, message: str) -> None:
    job.status = JobStatus.failed
    job.error_message = message
    _append_log(session, job, f"[FAIL] {message}")


def run_cloud_steps(job_id: int) -> None:
    """Runs group assignment + licensing for a job whose on-prem AD account
    was just created. Called as a FastAPI background task (fire-and-forget
    from the connector's point of view), so it opens its own DB session
    rather than reusing the request's - this can run for several minutes
    polling for sync, well past when the triggering HTTP request returned.

    Every step is soft-failure (logged, not raised) so one missing group
    doesn't block licensing or vice versa - same philosophy as the original desktop version's
    tool's per-group try/catch.
    """
    with Session(engine) as session:
        if not _try_claim_for_cloud_steps(session, job_id):
            return  # another attempt already holds a recent claim

        job = session.get(Job, job_id)
        if job is None:
            return
        config = session.get(Config, SINGLETON_ID)
        cred = session.get(GraphCredential, SINGLETON_ID)

        if cred is None:
            _fail(session, job, "No Graph app registration configured yet - cannot run cloud steps.")
            return

        try:
            token = _get_app_token(cred)
        except httpx.HTTPError as exc:
            _fail(session, job, f"Could not authenticate to Graph: {exc}")
            return

        user_id = None
        for attempt in range(1, SYNC_POLL_ATTEMPTS + 1):
            user_id = _find_user_id(token, job.ad_upn)
            if user_id is not None:
                break
            _append_log(
                session, job,
                f"[i] Waiting for {job.ad_upn} to sync to Entra "
                f"(check {attempt}/{SYNC_POLL_ATTEMPTS})...",
            )
            time.sleep(SYNC_POLL_INTERVAL_SECONDS)

        if user_id is None:
            _fail(session, job, f"User {job.ad_upn} never appeared in Graph - sync did not complete in time.")
            return

        _run_group_and_license_steps(session, job, config, token, user_id)


def _run_group_and_license_steps(session: Session, job: Job, config: Config, token: str, user_id: str) -> None:
    for rule in config.cloud_group_rules:
        rule_model = CloudGroupRule.model_validate(rule)
        if not _rule_matches(rule, job.joiner_fields):
            _append_log(session, job, f"[SKIP] {rule_model.display_name} - condition '{rule_model.condition}' not met.")
            continue
        group_id = _find_group_id(token, rule_model.display_name)
        if group_id is None:
            _append_log(session, job, f"[WARN] Group '{rule_model.display_name}' not found in Entra - skipped.")
            continue
        try:
            resp = httpx.post(
                f"{GRAPH_BASE}/groups/{group_id}/members/$ref",
                headers={"Authorization": f"Bearer {token}"},
                json={"@odata.id": f"{GRAPH_BASE}/directoryObjects/{user_id}"},
                timeout=30,
            )
            resp.raise_for_status()
            _append_log(session, job, f"[OK] Added to '{rule_model.display_name}'.")
        except httpx.HTTPError as exc:
            _append_log(session, job, f"[WARN] Failed to add to '{rule_model.display_name}': {exc}")

    site = job.joiner_fields.get("site", "")
    country = config.site_country_code.get(site, config.default_country_code)
    sku_part_numbers = [s for s in (config.primary_license_sku, config.default_license_sku) if s]
    if str(job.joiner_fields.get("mobile_device_requested", "")).strip().lower() == "yes" and config.mobile_license_sku:
        sku_part_numbers.append(config.mobile_license_sku)

    try:
        httpx.patch(
            f"{GRAPH_BASE}/users/{user_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"usageLocation": country},
            timeout=30,
        ).raise_for_status()
        if sku_part_numbers:
            sku_ids = _resolve_sku_ids(token, sku_part_numbers)
            if len(sku_ids) < len(sku_part_numbers):
                _append_log(session, job, "[WARN] One or more configured license SKUs aren't subscribed on this tenant.")
            if sku_ids:
                httpx.post(
                    f"{GRAPH_BASE}/users/{user_id}/assignLicense",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"addLicenses": [{"skuId": sku_id} for sku_id in sku_ids], "removeLicenses": []},
                    timeout=30,
                ).raise_for_status()
                _append_log(session, job, f"[OK] Licensed with: {', '.join(sku_part_numbers)}")
    except httpx.HTTPError as exc:
        _append_log(session, job, f"[WARN] Licensing step failed: {exc}")

    job.status = JobStatus.succeeded
    _append_log(session, job, "[OK] Hybrid provisioning complete.")
