# Getting a deployment live — step by step

Written for someone doing this for the first time. Each step says exactly
what to do and how to know it worked before moving to the next one. Don't
skip the "check it worked" line — every step here depends on the one
before it actually having succeeded.

Total time: budget a full day. Some of that is genuinely waiting (Azure
resources take real minutes to provision), and step 1 depends on someone
else's approval, not just your own effort.

**Haven't tested anything yet?** Start with
**[`TEST_AUTH_AND_LOGIC.md`](TEST_AUTH_AND_LOGIC.md)** — it validates
Graph auth and the group/license logic against a trial tenant in minutes,
with no VM and no cost. Once that passes,
**[`TEST_AD_SETUP.md`](TEST_AD_SETUP.md)** walks through standing up a
throwaway test AD forest in Azure and syncing it to a test Entra tenant,
so you have something real for steps 4 and 6 below before this ever
touches a real company's environment.

---

## Step 1 — Create the Entra app registration

*Why: this is the credential the backend uses to manage groups and
licenses in Microsoft 365. No live environment work can start without it.*

1. Go to [portal.azure.com](https://portal.azure.com) → search **App
   registrations** → **New registration**.
2. Name it something identifiable, e.g. `Hybrid Joiner Backend`. Leave the
   other defaults. Click **Register**.
3. On the app's overview page, copy and save two values somewhere safe:
   **Application (client) ID** and **Directory (tenant) ID**.
4. In the left menu, click **API permissions** → **Add a permission** →
   **Microsoft Graph** → **Application permissions** (not "Delegated" —
   easy to pick wrong by default). Search for and tick each of:
   - `User.ReadWrite.All`
   - `Group.ReadWrite.All`
   - `Organization.Read.All`
   Click **Add permissions**.
5. Still on that page, click **Grant admin consent**. If you're not a
   Global Admin, this is the point where you need someone who is — send
   them a link to this exact page and ask them to click that button.
   **Check it worked**: the Status column next to all three permissions
   shows a green check, not "Not granted".
6. In the left menu, click **Certificates & secrets** → **New client
   secret**. Any description, any expiry (shorter is more secure but means
   remembering to renew it — 12 months is a reasonable start). Click
   **Add**, then **immediately copy the "Value" column** — it's shown once
   and never again.

**You now have three values**: tenant ID, client ID, client secret. Keep
them somewhere secure (a password manager, not a text file) — step 3 needs
them.

---

## Step 2 — Deploy the Azure infrastructure

*Why: the backend needs somewhere to actually run.*

Follow **[`backend/DEPLOY_AZURE.md`](backend/DEPLOY_AZURE.md)** top to
bottom — it has the exact `az` CLI commands. Come back here once you've
confirmed the last command in that doc:

```bash
curl https://<your-app-name>.azurewebsites.net/health
```

**Check it worked**: that command returns `{"status":"ok"}`. If it
doesn't, don't move on — check `az webapp log tail --name <your-app-name>
--resource-group <your-rg>` for the actual startup error.

---

## Step 3 — Load your company's configuration into the backend

*Why: the backend now exists but knows nothing about your organization
yet — no OUs, no groups, no Graph credentials.*

Copy `config/config.example.yaml` to your own file (e.g.
`config/mycompany.yaml`) and fill in your real site OUs, group names,
domains and license SKUs. From a machine with Python 3 installed (your own
laptop is fine — this is a one-time setup call, nothing about it needs to
run continuously):

```bash
python3 scripts/configure_backend.py \
  --backend-url https://<your-app-name>.azurewebsites.net \
  --dashboard-token "<the DASHBOARD_TOKEN you set in step 2>" \
  --config config/mycompany.yaml \
  --aad-tenant-id "<tenant ID from step 1>" \
  --graph-client-id "<client ID from step 1>" \
  --graph-client-secret "<client secret from step 1>"
```

**Check it worked**: it prints `Config loaded from config/mycompany.yaml.`
and `Graph app registration credential stored.`, then two lines starting
`connector key` and `client key`.

**Copy both of those key values now — each is shown exactly once.** If you
lose one before finishing the next steps, re-run the same command with
`--reissue-keys-only` added to get fresh ones (this won't reload the
config, just issue new keys).

---

## Step 4 — Set up the connector on a domain-joined machine

*Why: this is the only piece that actually touches Active Directory —
everything else runs in Azure.*

1. Pick a machine: domain-joined, has the `ActiveDirectory` PowerShell
   module (part of RSAT). Not the Azure AD Connect server if you have
   another reasonable option — see the reasoning in this project's
   `README.md` if you want the "why" spelled out; short version, it's
   about not adding new standing capability to your most sensitive
   identity-sync box, not about that box lacking capacity.
2. Copy the whole `connector/` folder to that machine.
3. Ask whoever manages your AD delegation model to create a service
   account with rights scoped to *only*: creating user objects in the
   relevant OUs, and adding/removing members in the specific groups this
   tool manages. Not Domain Admin.
4. On that machine, open PowerShell **as Administrator** and run:

   ```powershell
   cd C:\path\to\connector
   .\Register-ConnectorTask.ps1 `
       -BackendUrl "https://<your-app-name>.azurewebsites.net" `
       -ConnectorKey "<the connector key from step 3>" `
       -ServiceAccount "YOURDOMAIN\svc-joiner-connector"
   ```

   You'll be prompted for that account's password.

**Check it worked**: the script prints `Last run result: 0`. Then confirm
the connector can actually reach the backend — from that same machine:

   ```powershell
   Invoke-RestMethod -Uri "https://<your-app-name>.azurewebsites.net/connector/config" `
       -Headers @{ Authorization = "Bearer <the connector key>" }
   ```

   This should print back the config you loaded in step 3 (site OUs,
   group names, etc.), not an error.

---

## Step 5 — Set up the admin team's tool

*Why: this is what your IT/HR team actually clicks to create a joiner.*

1. Copy `client/Client-Config.example.ps1` to `client/Client-Config.ps1`.
2. Open it and replace the two placeholder lines:
   ```powershell
   $BackendUrl = "https://<your-app-name>.azurewebsites.net"
   $ClientApiKey = "<the client key from step 3>"
   ```
3. Copy these three files to each admin's machine (or a shared drive
   everyone can reach), keeping them together in one folder:
   - `Joiner-Client.ps1`
   - `Client-Config.ps1` (the one you just created and edited)
   - `Launch-Joiner-Tool.bat`

**Check it worked**: double-click `Launch-Joiner-Tool.bat`. The tool
window should open with no error dialog. If you see a "Setup Error"
message box, it's telling you exactly what's wrong (missing config file,
or the placeholder values weren't replaced) — fix that before continuing.

---

## Step 6 — The actual test

*Why: nothing up to this point has touched a real AD account. This is
where you find out if it actually works.*

1. Use a **throwaway test identity** — not a real new hire. If you have a
   non-production OU or test area in AD, use it; if not, at minimum use an
   obviously-fake name so nobody mistakes the account for a real person's.
2. Fill out a copy of your joiner Excel form for that fake person.
3. Run it through `Joiner-Client.ps1` exactly as an admin would.
4. Watch the status box. Expect it to take a few minutes at minimum
   (mostly the Entra sync wait) — that's normal, not a hang.

**Check it worked**: you see `[OK] Hybrid provisioning complete.` at the
end, and the fake account exists in AD with the groups/license you'd
expect.

**Then clean up**: delete the test account from on-prem AD first, wait for
sync, then check Entra ID's deleted users and purge it from there too —
don't leave a fake account sitting around, even a test one.

---

## If something breaks

- **Step 1/2 issue**: check `az webapp log tail` (step 2) or the Entra
  portal's own error messages (step 1) — both usually say plainly what's
  wrong (a missing permission, a bad connection string).
- **Step 4 issue (connector)**: `Get-ScheduledTaskInfo -TaskName "Hybrid
  Joiner Connector"` shows whether it's running; check the service
  account actually has the AD rights it needs if jobs fail at the
  `New-ADUser` step.
- **Step 5/6 issue (GUI)**: the status box tells you which step failed and
  why — read the red line, it's written to be specific, not generic.
- **Genuinely stuck**: `GET /audit-log` and a job's `log_lines` (via `GET
  /jobs/{id}`) on the backend show exactly what happened and when — more
  reliable than trying to reconstruct it from memory.
