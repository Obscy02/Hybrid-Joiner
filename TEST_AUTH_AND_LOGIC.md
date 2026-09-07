# Testing auth + Graph logic against your real trial tenant

This is the fastest, cheapest thing to test first — **no VM, no AD forest,
no Azure spend, no connector**. It runs the backend on your own laptop and
exercises the two hardest parts of the whole system directly against your
real E5 trial tenant:

- **Auth**: does app-only (client credentials) Graph auth actually work
  with a real app registration and real admin consent?
- **Logic**: does rule evaluation, group assignment, and license
  assignment (`graph_actions.py`) actually do the right thing against a
  real tenant?

What this deliberately **doesn't** test: anything in `Connector-Agent.ps1`
— `New-ADUser`, OU resolution, on-prem group cloning. That needs a real AD,
which is what `TEST_AD_SETUP.md` is for, and can come after this passes.
The two are independent — this one is just much cheaper to do first, and
if something's broken in the Graph logic, better to find that out before
also paying for a VM.

---

## Step 1 — Register the Entra app in your trial tenant

Same as `GETTING_STARTED.md` step 1, done against your **own** trial
tenant (you're Global Admin of it, so admin consent is one click, no one
else to ask):

1. [portal.azure.com](https://portal.azure.com) → **App registrations** →
   **New registration**. Name it e.g. `Joiner Tool Test`.
2. Copy the **Application (client) ID** and **Directory (tenant) ID**.
3. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Application permissions** → add `User.ReadWrite.All`,
   `Group.ReadWrite.All`, `Organization.Read.All` → **Add permissions**.
4. Click **Grant admin consent for [your tenant]**. Confirm all three show
   a green check.
5. **Certificates & secrets** → **New client secret** → copy the **Value**
   immediately (shown once).

You now have: tenant ID, client ID, client secret.

## Step 2 — Run the backend locally

No Azure deploy needed for this test — it defaults to a local SQLite file
and a dev root token.

```bash
cd hybrid-onboarding-saas/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Check it worked**: `curl http://localhost:8000/health` returns
`{"status":"ok"}`.

Leave this running in its own terminal. It's using the default root token
`dev-only-change-me` (set by `DEV_DASHBOARD_TOKEN` in `auth.py`) — that's
fine for a localhost-only test, but never expose this port beyond your own
machine.

## Step 3 — Create one test group and one test user in the tenant

In [entra.microsoft.com](https://entra.microsoft.com) (your trial tenant):

1. **Groups** → **New group** → Security group, name it exactly
   `Test-Sales-Group` (matches the config below — use a different name if
   you like, just keep it consistent).
2. **Users** → **New user** → **Create new user**. Any display name, note
   the **UPN** it generates (e.g. `testuser1@yourtenant.onmicrosoft.com`).

This user stands in for "the account the connector would have created" —
since we're skipping AD entirely, we just need *a* real cloud user for
Graph to find.

## Step 4 — Find a real license SKU your trial actually has

Config needs a real `skuPartNumber`, and trial SKUs vary. Easiest way to
check: [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer),
sign in as yourself, run `GET https://graph.microsoft.com/v1.0/subscribedSkus`,
and note one `skuPartNumber` value from the response (e.g.
`SPE_E5`, `ENTERPRISEPREMIUM` — it depends on how your trial was
activated, don't assume the name).

## Step 5 — Load a minimal config into the local backend

Replace the placeholders (tenant ID/client ID/secret from step 1, the SKU
from step 4) and run:

```bash
BACKEND=http://localhost:8000
TOKEN=dev-only-change-me

curl -s -X PUT $BACKEND/config -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "site_country_code": {},
  "default_country_code": "GB",
  "cloud_group_rules": [
    {"key": "test_group", "display_name": "Test-Sales-Group", "condition": "always"}
  ],
  "primary_license_sku": "<SKU_PART_NUMBER_FROM_STEP_4>",
  "excel_field_map": {}
}'

curl -s -X PUT $BACKEND/graph-credential -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{
  "aad_tenant_id": "<TENANT_ID_FROM_STEP_1>",
  "client_id": "<CLIENT_ID_FROM_STEP_1>",
  "client_secret": "<CLIENT_SECRET_FROM_STEP_1>"
}'
```

**Check it worked**: both return the object back (config) or `{"ok":
true}` (credential), not an error.

## Step 6 — Issue a client key and a connector key

```bash
curl -s -X POST "$BACKEND/keys?label=test-client&role=client" -H "Authorization: Bearer $TOKEN"
curl -s -X POST "$BACKEND/keys?label=test-connector&role=connector" -H "Authorization: Bearer $TOKEN"
```

Each prints an `api_key` value once — save both.

## Step 7 — Create a job and skip straight to "on-prem done"

Normally the connector claims a job, runs `New-ADUser`, then calls
`onprem-complete`. Since we're testing the Graph half in isolation, we
create the job and call `onprem-complete` directly with the test user's
real UPN from step 3 — the backend doesn't care how a job got to that
state, only what happens next.

```bash
CLIENT_KEY="<client key from step 6>"
CONNECTOR_KEY="<connector key from step 6>"

JOB=$(curl -s -X POST $BACKEND/jobs -H "Authorization: Bearer $CLIENT_KEY" -H "Content-Type: application/json" -d '{
  "full_name": "Test Colleague",
  "is_line_manager": "No"
}')
echo $JOB
JOB_ID=$(echo $JOB | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -s -X POST "$BACKEND/connector/jobs/$JOB_ID/onprem-complete?ad_samaccountname=testuser1&ad_upn=<UPN_FROM_STEP_3>" \
  -H "Authorization: Bearer $CONNECTOR_KEY"
```

This flips the job to `awaiting_cloud_steps` and schedules the real Graph
work (auth, group add, license assign) as a background task.

## Step 8 — Watch it happen

```bash
watch -n2 "curl -s $BACKEND/jobs/$JOB_ID -H 'Authorization: Bearer $CLIENT_KEY' | python3 -m json.tool"
```

Because this is a real cloud-only user (not something waiting on AD sync),
`_find_user_id` should succeed on the very first attempt — this should
finish in seconds, not the 15-minute sync-wait budget that's there for the
real hybrid case.

**Check it worked**: `log_lines` ends with `[OK] Hybrid provisioning
complete.`, and in the Entra portal, `testuser1` is now a member of
`Test-Sales-Group` and has the license from step 4 assigned.

If something's wrong, the log lines say exactly which step failed
(`[FAIL] Could not authenticate to Graph: ...` means step 1's permissions
or consent are off; `[WARN] Group 'Test-Sales-Group' not found` means a
typo between step 3 and step 5).

## When you're done

- Remove the test user's license and group membership, then delete the
  test user and test group from the tenant.
- Stop the `uvicorn` process (Ctrl+C) and delete `backend/saas_dev.db` if
  you don't want the test data lying around.
- Delete the test app registration if you don't plan to reuse it.

## Next step after this passes

This proves the cloud half works. `TEST_AD_SETUP.md` is the next
increment — it adds a real AD forest and Azure AD Connect so the same
config can be driven end-to-end by an actual `New-ADUser` call and real
sync, instead of the manually-created test user this guide used as a
stand-in.
