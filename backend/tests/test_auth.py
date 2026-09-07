"""
Auth boundary tests: the three credential types (dashboard token, client
key, connector key) must not be interchangeable. This is the property
that makes a compromised admin-GUI laptop unable to do AD writes, and a
compromised connector machine unable to reconfigure the deployment.
"""


def test_dashboard_route_rejects_no_auth(client):
    resp = client.get("/config")
    assert resp.status_code == 401


def test_dashboard_route_rejects_wrong_token(client):
    resp = client.get("/config", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_dashboard_route_accepts_real_token(client, dashboard_headers):
    resp = client.get("/config", headers=dashboard_headers)
    # 404 (not yet configured) still proves the token itself was accepted.
    assert resp.status_code in (200, 404)


def test_client_key_cannot_reach_connector_routes(configured_client):
    resp = configured_client["client"].post("/connector/jobs/claim", headers=configured_client["client_headers"])
    assert resp.status_code == 401


def test_connector_key_cannot_reach_client_routes(configured_client):
    resp = configured_client["client"].post(
        "/jobs", headers=configured_client["connector_headers"], json={"full_name": "Test"}
    )
    assert resp.status_code == 401


def test_client_key_cannot_reach_dashboard_routes(configured_client):
    resp = configured_client["client"].get("/keys", headers=configured_client["client_headers"])
    assert resp.status_code == 401


def test_revoked_key_rejected_immediately(configured_client, dashboard_headers):
    client = configured_client["client"]
    keys = client.get("/keys", headers=dashboard_headers).json()
    connector_key_id = next(k["id"] for k in keys if k["role"] == "connector")

    # Works before revocation.
    assert client.post("/connector/jobs/claim", headers=configured_client["connector_headers"]).status_code == 200

    revoke_resp = client.post(f"/keys/{connector_key_id}/revoke", headers=dashboard_headers)
    assert revoke_resp.status_code == 200

    # Same key, same request - now rejected.
    resp = client.post("/connector/jobs/claim", headers=configured_client["connector_headers"])
    assert resp.status_code == 401


def test_revoking_one_key_does_not_affect_another(configured_client, dashboard_headers):
    client = configured_client["client"]
    keys = client.get("/keys", headers=dashboard_headers).json()
    connector_key_id = next(k["id"] for k in keys if k["role"] == "connector")

    client.post(f"/keys/{connector_key_id}/revoke", headers=dashboard_headers)

    # The unrelated client key still works.
    resp = client.get("/jobs/999999", headers=configured_client["client_headers"])
    assert resp.status_code == 404  # not 401 - auth passed, job just doesn't exist


def test_named_admin_key_can_do_admin_things(client, dashboard_headers):
    """The root DASHBOARD_TOKEN can issue a named admin key, and that key
    works for the same routes root does - the point of admin keys is
    individual accountability, not reduced capability.
    """
    issue_resp = client.post("/keys", headers=dashboard_headers, params={"label": "Alex", "role": "admin"})
    admin_key = issue_resp.json()["api_key"]
    admin_headers = {"Authorization": f"Bearer {admin_key}"}

    resp = client.get("/keys", headers=admin_headers)
    assert resp.status_code == 200


def test_admin_key_actions_attributed_by_name_in_audit_log(client, dashboard_headers):
    issue_resp = client.post("/keys", headers=dashboard_headers, params={"label": "Alex", "role": "admin"})
    admin_key = issue_resp.json()["api_key"]
    admin_headers = {"Authorization": f"Bearer {admin_key}"}

    client.post("/keys", headers=admin_headers, params={"label": "some-connector", "role": "connector"})

    entries = client.get("/audit-log", headers=dashboard_headers).json()
    admin_actions = [e for e in entries if e["action"] == "key.issue" and "some-connector" in e["detail"]]
    assert len(admin_actions) == 1
    assert admin_actions[0]["actor"] == "admin:Alex"


def test_revoked_admin_key_loses_access(client, dashboard_headers):
    issue_resp = client.post("/keys", headers=dashboard_headers, params={"label": "Alex", "role": "admin"})
    admin_key_id = issue_resp.json()["key_id"]
    admin_headers = {"Authorization": f"Bearer {issue_resp.json()['api_key']}"}

    client.post(f"/keys/{admin_key_id}/revoke", headers=dashboard_headers)

    resp = client.get("/keys", headers=admin_headers)
    assert resp.status_code == 401


def test_root_token_still_works_alongside_named_admin_keys(client, dashboard_headers):
    """Root is a break-glass credential, not replaced by admin keys -
    losing access to every named admin key shouldn't lock the operator out
    entirely.
    """
    client.post("/keys", headers=dashboard_headers, params={"label": "Alex", "role": "admin"})
    resp = client.get("/keys", headers=dashboard_headers)
    assert resp.status_code == 200
