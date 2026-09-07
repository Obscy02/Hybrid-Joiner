"""
The cloud-steps logic, tested two ways:
  - _rule_matches: pure unit tests, no I/O at all - this is what encodes
    a deployment's actual business rules (CA/VPN group choice by pattern,
    SSO placeholder-text detection, etc.) and is worth pinning down
    precisely.
  - run_cloud_steps: full integration tests with every Microsoft Graph
    call intercepted by respx - no real tenant, no network, no cost, but
    exercises the actual httpx request/response handling code, not a
    reimplementation of it.
"""
import pytest
import respx
from httpx import Response
from sqlmodel import Session

from app.graph_actions import _rule_matches, find_interrupted_jobs, run_cloud_steps
from app.models import Job, JobStatus


# ---- _rule_matches ---------------------------------------------------

@pytest.mark.parametrize(
    "condition,fields,expected",
    [
        ("always", {}, True),
        ("is_line_manager", {"is_line_manager": "Yes"}, True),
        ("is_line_manager", {"is_line_manager": "No"}, False),
        ("is_line_manager", {}, False),
        ("has_mobile_device", {"mobile_device_requested": "yes"}, True),  # case-insensitive
        ("has_mobile_device", {"mobile_device_requested": "No"}, False),
        ("field:cbp_role", {"cbp_role": "Analyst"}, True),
        ("field:cbp_role", {"cbp_role": ""}, False),
        ("field:cbp_role", {"cbp_role": "Complete if CBP required"}, False),
        ("field:cbp_role", {"cbp_role": "To be completed"}, False),
        ("field:cbp_role", {"cbp_role": "Not required"}, False),
        ("field:cbp_role", {"cbp_role": "N/A"}, False),
        ("field_matches:site:Meridian|Castlebellingham", {"site": "Castlebellingham"}, True),
        ("field_matches:site:Meridian|Castlebellingham", {"site": "Belfast"}, False),
        ("field_not_matches:site:Meridian|Castlebellingham", {"site": "Belfast"}, True),
        ("field_not_matches:site:Meridian|Castlebellingham", {"site": "Castlebellingham"}, False),
        ("field_matches:department:IS|IT|Cyber", {"department": "IT Support"}, True),
        ("unknown_condition_type", {}, False),
    ],
)
def test_rule_matches(condition, fields, expected):
    assert _rule_matches({"condition": condition}, fields) is expected


# ---- run_cloud_steps ---------------------------------------------------

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/test-aad-tenant/oauth2/v2.0/token"


def _make_job(session: Session, **overrides) -> Job:
    defaults = dict(
        status=JobStatus.awaiting_cloud_steps,
        ad_upn="jamie.test@example.com",
        joiner_fields={"site": "Belfast", "department": "IT", "mobile_device_requested": "Yes"},
    )
    defaults.update(overrides)
    job = Job(**defaults)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _configure(client, dashboard_headers, cloud_group_rules):
    client.put(
        "/config",
        headers=dashboard_headers,
        json={
            "site_ou_fallback": {}, "company_domain_fallback": {}, "site_phone_fallback": {},
            "site_country_code": {"Belfast": "GB"}, "default_country_code": "GB",
            "excluded_group_patterns": [], "line_manager_group": None,
            "cloud_group_rules": cloud_group_rules,
            "primary_license_sku": "ENTERPRISEPACK", "mobile_license_sku": "EMSPREMIUM",
            "default_license_sku": None, "excel_field_map": {}, "ad_sync_server": None,
        },
    )
    client.put(
        "/graph-credential",
        headers=dashboard_headers,
        json={"aad_tenant_id": "test-aad-tenant", "client_id": "cid", "client_secret": "secret"},
    )


@respx.mock
def test_run_cloud_steps_success_path(client, dashboard_headers, session):
    _configure(
        client, dashboard_headers,
        cloud_group_rules=[
            {"key": "all", "display_name": "All Staff", "condition": "always"},
            {"key": "mobile", "display_name": "Mobile Users", "condition": "has_mobile_device"},
        ],
    )
    job = _make_job(session)

    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "fake-token"}))
    respx.get(f"{GRAPH}/users/{job.ad_upn}").mock(return_value=Response(200, json={"id": "user-123"}))
    respx.get(f"{GRAPH}/groups", params={"$filter": "displayName eq 'All Staff'"}).mock(
        return_value=Response(200, json={"value": [{"id": "group-all"}]})
    )
    respx.get(f"{GRAPH}/groups", params={"$filter": "displayName eq 'Mobile Users'"}).mock(
        return_value=Response(200, json={"value": [{"id": "group-mobile"}]})
    )
    respx.post(f"{GRAPH}/groups/group-all/members/$ref").mock(return_value=Response(204))
    respx.post(f"{GRAPH}/groups/group-mobile/members/$ref").mock(return_value=Response(204))
    respx.patch(f"{GRAPH}/users/user-123").mock(return_value=Response(200))
    respx.get(f"{GRAPH}/subscribedSkus").mock(
        return_value=Response(200, json={"value": [
            {"skuPartNumber": "ENTERPRISEPACK", "skuId": "sku-1"},
            {"skuPartNumber": "EMSPREMIUM", "skuId": "sku-2"},
        ]})
    )
    respx.post(f"{GRAPH}/users/user-123/assignLicense").mock(return_value=Response(200))

    run_cloud_steps(job.id)

    session.refresh(job)
    assert job.status == JobStatus.succeeded
    assert any("Added to 'All Staff'" in line for line in job.log_lines)
    assert any("Added to 'Mobile Users'" in line for line in job.log_lines)
    assert any("Licensed with: ENTERPRISEPACK, EMSPREMIUM" in line for line in job.log_lines)
    assert job.log_lines[-1] == "[OK] Hybrid provisioning complete."


@respx.mock
def test_run_cloud_steps_missing_credential_fails_cleanly(client, dashboard_headers, session):
    _configure(client, dashboard_headers, cloud_group_rules=[])
    # Overwrite config but never set a graph-credential - simulate a
    # deployment that hasn't finished setup yet.
    from app.models import GraphCredential, SINGLETON_ID
    existing = session.get(GraphCredential, SINGLETON_ID)
    if existing:
        session.delete(existing)
        session.commit()

    job = _make_job(session)
    run_cloud_steps(job.id)

    session.refresh(job)
    assert job.status == JobStatus.failed
    assert "No Graph app registration configured" in job.error_message


@respx.mock
def test_run_cloud_steps_bad_credentials_fails_cleanly(client, dashboard_headers, session):
    _configure(client, dashboard_headers, cloud_group_rules=[])
    job = _make_job(session)

    respx.post(TOKEN_URL).mock(return_value=Response(400, json={"error": "invalid_client"}))

    run_cloud_steps(job.id)

    session.refresh(job)
    assert job.status == JobStatus.failed
    assert "Could not authenticate to Graph" in job.error_message


@respx.mock
def test_run_cloud_steps_user_never_syncs_times_out(client, dashboard_headers, session, monkeypatch):
    import app.graph_actions as ga
    monkeypatch.setattr(ga, "SYNC_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(ga, "SYNC_POLL_INTERVAL_SECONDS", 0)  # don't actually wait 15 minutes in a test

    _configure(client, dashboard_headers, cloud_group_rules=[])
    job = _make_job(session)

    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "fake-token"}))
    respx.get(f"{GRAPH}/users/{job.ad_upn}").mock(return_value=Response(404))

    run_cloud_steps(job.id)

    session.refresh(job)
    assert job.status == JobStatus.failed
    assert "never appeared in Graph" in job.error_message
    assert sum("Waiting for" in line for line in job.log_lines) == 2


@respx.mock
def test_run_cloud_steps_missing_group_is_soft_failure(client, dashboard_headers, session):
    """A group that doesn't exist in Entra shouldn't block licensing or
    other groups - same per-step soft-failure philosophy as the original
    desktop tool.
    """
    _configure(
        client, dashboard_headers,
        cloud_group_rules=[{"key": "ghost", "display_name": "Does Not Exist", "condition": "always"}],
    )
    job = _make_job(session, joiner_fields={"site": "Belfast", "department": "IT"})

    respx.post(TOKEN_URL).mock(return_value=Response(200, json={"access_token": "fake-token"}))
    respx.get(f"{GRAPH}/users/{job.ad_upn}").mock(return_value=Response(200, json={"id": "user-123"}))
    respx.get(f"{GRAPH}/groups").mock(return_value=Response(200, json={"value": []}))
    respx.patch(f"{GRAPH}/users/user-123").mock(return_value=Response(200))
    respx.get(f"{GRAPH}/subscribedSkus").mock(return_value=Response(200, json={"value": []}))

    run_cloud_steps(job.id)

    session.refresh(job)
    assert job.status == JobStatus.succeeded  # missing group didn't block completion
    assert any("not found in Entra" in line for line in job.log_lines)


# ---- find_interrupted_jobs ---------------------------------------------

def test_find_interrupted_jobs_only_returns_awaiting_cloud_steps(session):
    stuck = _make_job(session)
    _make_job(session, status=JobStatus.pending, ad_upn="pending@example.com")
    _make_job(session, status=JobStatus.succeeded, ad_upn="done@example.com")

    result = find_interrupted_jobs(session)

    assert result == [stuck.id]


def test_find_interrupted_jobs_excludes_recently_claimed(session):
    """A job another (presumably still-running) attempt claimed a moment
    ago should NOT be treated as interrupted - only a stale or absent
    claim counts. Otherwise two backend instances polling for stuck jobs
    at the same moment would both pick up a perfectly healthy in-progress
    job.
    """
    from app.graph_actions import _try_claim_for_cloud_steps

    job = _make_job(session)
    claimed = _try_claim_for_cloud_steps(session, job.id)
    assert claimed is True

    assert find_interrupted_jobs(session) == []


# ---- _try_claim_for_cloud_steps (concurrency safety) --------------------

def test_claim_is_exclusive_second_caller_loses(session):
    from app.graph_actions import _try_claim_for_cloud_steps

    job = _make_job(session)

    first = _try_claim_for_cloud_steps(session, job.id)
    second = _try_claim_for_cloud_steps(session, job.id)

    assert first is True
    assert second is False  # this is the guarantee that stops double-processing


def test_stale_claim_can_be_reclaimed(session, monkeypatch):
    """A claim older than CLOUD_STEPS_CLAIM_STALE_AFTER (the attempt that
    made it is presumed dead) can be taken over by a new attempt - this is
    what lets a genuinely interrupted job actually get resumed, rather
    than being permanently locked by a claim nobody will ever finish.
    """
    import app.graph_actions as ga
    from app.graph_actions import _try_claim_for_cloud_steps

    monkeypatch.setattr(ga, "CLOUD_STEPS_CLAIM_STALE_AFTER", ga.timedelta(minutes=20))

    job = _make_job(session)
    job.cloud_steps_claimed_at = ga._utcnow() - ga.timedelta(minutes=30)  # older than the stale window
    session.add(job)
    session.commit()

    reclaimed = _try_claim_for_cloud_steps(session, job.id)
    assert reclaimed is True


def test_fresh_claim_cannot_be_reclaimed(session, monkeypatch):
    import app.graph_actions as ga
    from app.graph_actions import _try_claim_for_cloud_steps

    monkeypatch.setattr(ga, "CLOUD_STEPS_CLAIM_STALE_AFTER", ga.timedelta(minutes=20))

    job = _make_job(session)
    job.cloud_steps_claimed_at = ga._utcnow() - ga.timedelta(minutes=1)  # well within the stale window
    session.add(job)
    session.commit()

    reclaimed = _try_claim_for_cloud_steps(session, job.id)
    assert reclaimed is False
