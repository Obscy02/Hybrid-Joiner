"""
Config/credential/key management: the "operator" side of the API. Also
covers the exact session-expiry bug class that bit this project twice for
real (a second commit - here, the audit log write - silently expiring an
already-refreshed SQLModel object before response serialization).
"""
import json

MINIMAL_CONFIG = {
    "site_ou_fallback": {},
    "company_domain_fallback": {},
    "site_phone_fallback": {},
    "site_country_code": {},
    "default_country_code": "GB",
    "excluded_group_patterns": [],
    "line_manager_group": None,
    "cloud_group_rules": [],
    "primary_license_sku": None,
    "mobile_license_sku": None,
    "default_license_sku": None,
    "excel_field_map": {},
    "ad_sync_server": None,
}


def test_config_missing_before_first_put(client, dashboard_headers):
    resp = client.get("/config", headers=dashboard_headers)
    assert resp.status_code == 404


def test_put_config_creates_then_returns_real_data(client, dashboard_headers):
    """Regression test for the exact bug found twice in this project: a
    response_model=X route returning `{}` because a commit after the
    object was already refreshed silently expired it. If this bug were
    reintroduced, `resp.json()["default_country_code"]` would be missing
    instead of "GB".
    """
    resp = client.put("/config", headers=dashboard_headers, json={**MINIMAL_CONFIG, "default_country_code": "GB"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_country_code"] == "GB"
    assert body["id"] == 1


def test_put_config_twice_upserts_in_place(client, dashboard_headers):
    client.put("/config", headers=dashboard_headers, json={**MINIMAL_CONFIG, "default_country_code": "GB"})
    resp = client.put("/config", headers=dashboard_headers, json={**MINIMAL_CONFIG, "default_country_code": "IE"})
    assert resp.status_code == 200
    assert resp.json()["default_country_code"] == "IE"
    assert resp.json()["id"] == 1  # still the same singleton row, not a second one


def test_put_config_rejects_malformed_cloud_group_rule(client, dashboard_headers):
    """A rule missing display_name should be rejected with a 422 that names
    the problem, not silently stored - see routes/admin.py's put_config for
    why this can't just be a type annotation on the model.
    """
    resp = client.put(
        "/config",
        headers=dashboard_headers,
        json={**MINIMAL_CONFIG, "cloud_group_rules": [{"key": "bad"}]},
    )
    assert resp.status_code == 422
    assert "cloud_group_rules[0]" in resp.json()["detail"]

    # And the singleton was never created/updated with the bad data.
    assert client.get("/config", headers=dashboard_headers).status_code == 404


def test_graph_credential_secret_never_returned_or_listed(client, dashboard_headers):
    client.put(
        "/graph-credential",
        headers=dashboard_headers,
        json={"aad_tenant_id": "t1", "client_id": "c1", "client_secret": "super-secret-value"},
    )
    # No endpoint should ever echo the secret back.
    config_resp = client.get("/config", headers=dashboard_headers)
    audit_resp = client.get("/audit-log", headers=dashboard_headers)
    assert "super-secret-value" not in config_resp.text
    assert "super-secret-value" not in audit_resp.text


def test_issue_key_returns_plaintext_once_list_never_does(client, dashboard_headers):
    issue_resp = client.post("/keys", headers=dashboard_headers, params={"label": "test", "role": "client"})
    assert issue_resp.status_code == 200
    plaintext = issue_resp.json()["api_key"]
    assert plaintext

    list_resp = client.get("/keys", headers=dashboard_headers)
    assert list_resp.status_code == 200
    assert plaintext not in json.dumps(list_resp.json())
    assert "key_hash" not in json.dumps(list_resp.json())


def test_audit_log_records_actions_in_order(client, dashboard_headers):
    client.put("/config", headers=dashboard_headers, json=MINIMAL_CONFIG)
    client.post("/keys", headers=dashboard_headers, params={"label": "a", "role": "client"})

    entries = client.get("/audit-log", headers=dashboard_headers).json()
    actions = [e["action"] for e in entries]
    assert "config.update" in actions
    assert "key.issue" in actions
    # Most recent first.
    assert entries[0]["created_at"] >= entries[-1]["created_at"]
