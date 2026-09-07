#!/usr/bin/env python3
"""
One-time setup script for a freshly deployed backend: loads a config YAML
file (see config/config.example.yaml for the shape), stores the Graph app
registration credential, and issues one connector key and one client key.

This is the only thing that knows a specific deployment's real OUs, group
names, domains and phone numbers - which live in your own copy of the
config YAML, not in this script or anywhere else in the repo.

Usage:
    python3 scripts/configure_backend.py \\
        --backend-url https://your-backend-host \\
        --dashboard-token <the real DASHBOARD_TOKEN, changed from the default> \\
        --config config/your-company.yaml \\
        --aad-tenant-id <Entra tenant GUID> \\
        --graph-client-id <app registration client id> \\
        --graph-client-secret <app registration client secret>

Prints the connector key and client key you paste into the connector's
launch parameters and the admin GUI's config file respectively. Each is
shown exactly once - if you lose one, re-run with `--reissue-keys-only`
rather than trying to recover it.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

import yaml


def call(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {url} -> {exc.code}: {exc.read().decode()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--dashboard-token", required=True)
    parser.add_argument("--config", default="config/config.example.yaml", help="Path to this deployment's config YAML")
    parser.add_argument("--aad-tenant-id", required=True)
    parser.add_argument("--graph-client-id", required=True)
    parser.add_argument("--graph-client-secret", required=True)
    parser.add_argument(
        "--reissue-keys-only", action="store_true",
        help="Skip loading config/credentials again - just issue fresh connector/client keys",
    )
    args = parser.parse_args()

    base = args.backend_url.rstrip("/")
    token = args.dashboard_token

    if not args.reissue_keys_only:
        with open(args.config) as f:
            config = yaml.safe_load(f)

        call("PUT", f"{base}/config", token, config)
        print(f"Config loaded from {args.config}.")

        call(
            "PUT", f"{base}/graph-credential", token,
            {
                "aad_tenant_id": args.aad_tenant_id,
                "client_id": args.graph_client_id,
                "client_secret": args.graph_client_secret,
            },
        )
        print("Graph app registration credential stored.")

    connector_resp = call("POST", f"{base}/keys?label=main-connector&role=connector", token)
    client_resp = call("POST", f"{base}/keys?label=admin-team-gui&role=client", token)

    print("\n--- Save these now, they will not be shown again ---")
    print(f"connector key  : {connector_resp['api_key']}   (paste into the connector's launch parameters)")
    print(f"client key     : {client_resp['api_key']}   (paste into client/Client-Config.ps1)")


if __name__ == "__main__":
    main()
