"""
Shared fixtures. Everything here runs against a throwaway SQLite database
that's deleted after every test - no Postgres, no Azure, no network,
$0 cost, no live environment of any kind required.

Microsoft Graph itself is mocked per-test with respx (see test_graph_
actions.py) rather than here, since which endpoints matter differs test
to test.
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from app import db as db_module
from app import graph_actions as graph_actions_module
from app import rate_limit
from app.auth import DEV_DASHBOARD_TOKEN
from app.main import app


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """The auth-failure lockout tracks state at module level (see
    rate_limit.py's docstring for why) rather than per-app-instance, and
    the `app` object itself is a module-level singleton shared by every
    TestClient across the whole test session. Without this, one test's
    401s would count towards every other test's lockout threshold.
    """
    rate_limit.reset_state()
    yield
    rate_limit.reset_state()


@pytest.fixture
def test_engine(monkeypatch):
    """A fresh, throwaway SQLite database file per test - not `sqlite:///
    :memory:` with StaticPool, even though that's the more common pattern
    for quick test setups.

    That combination forces every thread onto one single shared physical
    connection (StaticPool + check_same_thread=False disables Python's
    same-thread check without making concurrent use of one connection
    actually safe). This project's periodic sweep and background
    run_cloud_steps tasks genuinely run on separate threads even inside a
    test (see test_recovery.py), and that combination produced a real,
    intermittent segfault under pytest - not a hypothetical risk. A file-
    based database lets each thread get its own connection from a normal
    pool, which is safe, and which is also a closer match to how Postgres
    (a real connection per thread/session) behaves in production, where
    this class of bug wouldn't exist in the first place.

    graph_actions.py imported `engine` by name at module load time, so
    patching db.engine alone wouldn't affect it - both names have to be
    repointed at the same test engine for a test to see consistent data
    whether it goes through the API or through graph_actions directly.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(graph_actions_module, "engine", engine)
    yield engine
    engine.dispose()
    os.unlink(path)


@pytest.fixture
def session(test_engine):
    with Session(test_engine) as s:
        yield s


@pytest.fixture
def client(test_engine):
    """Entering TestClient as a context manager runs the app's lifespan
    (including the interrupted-job recovery sweep) - important for
    test_recovery.py, and just correct practice generally.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture
def dashboard_headers():
    return {"Authorization": f"Bearer {DEV_DASHBOARD_TOKEN}"}


@pytest.fixture
def configured_client(client, dashboard_headers):
    """A client with a minimal but complete config + Graph credential
    already loaded, plus one connector key and one client key - the
    starting point most job-lifecycle tests need.
    """
    client.put(
        "/config",
        headers=dashboard_headers,
        json={
            "site_ou_fallback": {},
            "company_domain_fallback": {"Example Co": "example.com"},
            "site_phone_fallback": {},
            "site_country_code": {},
            "default_country_code": "GB",
            "excluded_group_patterns": [],
            "line_manager_group": "AllLineManagers",
            "cloud_group_rules": [
                {"key": "all_staff", "display_name": "All Staff", "condition": "always"},
            ],
            "primary_license_sku": "ENTERPRISEPACK",
            "mobile_license_sku": None,
            "default_license_sku": None,
            "excel_field_map": {"full_name": "E6", "site": "E9"},
            "ad_sync_server": None,
        },
    )
    client.put(
        "/graph-credential",
        headers=dashboard_headers,
        json={"aad_tenant_id": "test-aad-tenant", "client_id": "test-client-id", "client_secret": "test-secret"},
    )
    connector_key = client.post(
        "/keys", headers=dashboard_headers, params={"label": "test-connector", "role": "connector"}
    ).json()["api_key"]
    client_key = client.post(
        "/keys", headers=dashboard_headers, params={"label": "test-client", "role": "client"}
    ).json()["api_key"]

    return {"client": client, "connector_headers": {"Authorization": f"Bearer {connector_key}"},
            "client_headers": {"Authorization": f"Bearer {client_key}"}}
