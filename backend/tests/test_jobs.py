"""
Job lifecycle: creation (JSON and Excel upload), claim atomicity, logging,
and the connector-facing routes' 404-on-someone-else's-job behavior.
"""
import io

import openpyxl


def _make_excel(cells: dict) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for ref, value in cells.items():
        ws[ref] = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_create_job_from_json(configured_client):
    resp = configured_client["client"].post(
        "/jobs", headers=configured_client["client_headers"], json={"full_name": "Jamie Test", "site": "Main Office"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["joiner_fields"]["full_name"] == "Jamie Test"


def test_preview_excel_does_not_create_a_job(configured_client):
    client = configured_client["client"]
    excel_bytes = _make_excel({"E6": "Alex Doe", "E9": "Main Office"})

    resp = client.post(
        "/preview-excel", headers=configured_client["client_headers"], files={"file": ("form.xlsx", excel_bytes)}
    )
    assert resp.status_code == 200
    assert resp.json() == {"full_name": "Alex Doe", "site": "Main Office"}

    # Claiming should find nothing - preview never created a Job row.
    claim = client.post("/connector/jobs/claim", headers=configured_client["connector_headers"])
    assert claim.json() is None


def test_create_job_from_excel_then_claim(configured_client):
    client = configured_client["client"]
    excel_bytes = _make_excel({"E6": "Sam Test", "E9": "Main Office"})

    create_resp = client.post(
        "/jobs/from-excel", headers=configured_client["client_headers"], files={"file": ("form.xlsx", excel_bytes)}
    )
    assert create_resp.status_code == 200
    job_id = create_resp.json()["id"]

    claim_resp = client.post("/connector/jobs/claim", headers=configured_client["connector_headers"])
    assert claim_resp.status_code == 200
    claimed = claim_resp.json()
    assert claimed["id"] == job_id
    assert claimed["status"] == "claimed"
    assert claimed["joiner_fields"]["full_name"] == "Sam Test"


def test_claim_is_atomic_second_poll_gets_nothing(configured_client):
    client = configured_client["client"]
    client.post("/jobs", headers=configured_client["client_headers"], json={"full_name": "Only One"})

    first = client.post("/connector/jobs/claim", headers=configured_client["connector_headers"])
    second = client.post("/connector/jobs/claim", headers=configured_client["connector_headers"])

    assert first.json() is not None
    assert second.json() is None


def test_claim_returns_oldest_pending_job_first(configured_client):
    client = configured_client["client"]
    client.post("/jobs", headers=configured_client["client_headers"], json={"full_name": "First"})
    client.post("/jobs", headers=configured_client["client_headers"], json={"full_name": "Second"})

    claimed = client.post("/connector/jobs/claim", headers=configured_client["connector_headers"]).json()
    assert claimed["joiner_fields"]["full_name"] == "First"


def test_append_log_appears_in_job_status(configured_client):
    client = configured_client["client"]
    job_id = client.post("/jobs", headers=configured_client["client_headers"], json={"full_name": "X"}).json()["id"]
    client.post("/connector/jobs/claim", headers=configured_client["connector_headers"])

    client.post(
        f"/connector/jobs/{job_id}/log",
        headers=configured_client["connector_headers"],
        params={"line": "[OK] AD user created successfully."},
    )

    status = client.get(f"/jobs/{job_id}", headers=configured_client["client_headers"]).json()
    assert status["status"] == "running"
    assert "[OK] AD user created successfully." in status["log_lines"]


def test_fail_job_sets_status_and_error(configured_client):
    client = configured_client["client"]
    job_id = client.post("/jobs", headers=configured_client["client_headers"], json={"full_name": "X"}).json()["id"]

    client.post(
        f"/connector/jobs/{job_id}/fail",
        headers=configured_client["connector_headers"],
        params={"error_message": "New-ADUser failed: name collision"},
    )

    status = client.get(f"/jobs/{job_id}", headers=configured_client["client_headers"]).json()
    assert status["status"] == "failed"
    assert status["error_message"] == "New-ADUser failed: name collision"


def test_get_nonexistent_job_is_404(configured_client):
    resp = configured_client["client"].get("/jobs/999999", headers=configured_client["client_headers"])
    assert resp.status_code == 404


def test_from_excel_without_configured_field_map_is_400(client, dashboard_headers):
    """A deployment that's had /config PUT but never given an
    excel_field_map should fail clearly, not silently create a job with
    every field blank.
    """
    client.put(
        "/config",
        headers=dashboard_headers,
        json={
            "site_ou_fallback": {}, "company_domain_fallback": {}, "site_phone_fallback": {},
            "site_country_code": {}, "default_country_code": "GB", "excluded_group_patterns": [],
            "line_manager_group": None, "cloud_group_rules": [], "primary_license_sku": None,
            "mobile_license_sku": None, "default_license_sku": None, "excel_field_map": {}, "ad_sync_server": None,
        },
    )
    client_key = client.post(
        "/keys", headers=dashboard_headers, params={"label": "c", "role": "client"}
    ).json()["api_key"]

    resp = client.post(
        "/jobs/from-excel",
        headers={"Authorization": f"Bearer {client_key}"},
        files={"file": ("form.xlsx", _make_excel({"E6": "X"}))},
    )
    assert resp.status_code == 400
