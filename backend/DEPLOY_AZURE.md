# Deploying the backend to Azure

Targets Azure Container Registry (to host the image) + Azure Database for
PostgreSQL Flexible Server (real persistence, not SQLite) + Azure App
Service for Containers (runs it). All commands use the `az` CLI - run
`az login` first. Replace `<...>` placeholders; names below assume you'll
substitute your own.

## 1. Resource group + registry

```bash
az group create --name joiner-rg --location uksouth

az acr create --resource-group joiner-rg --name joinerregistry --sku Basic
az acr login --name joinerregistry
```

## 2. Build and push the image

From `backend/`:

```bash
az acr build --registry joinerregistry --image hybrid-joiner-backend:latest .
```

(`az acr build` builds in the cloud - no local Docker install needed,
which matters since this wasn't tested locally in a sandbox without
Docker either.)

## 3. Postgres

```bash
az postgres flexible-server create \
  --resource-group joiner-rg \
  --name joiner-db \
  --admin-user joineradmin \
  --admin-password "<CHOOSE-A-REAL-PASSWORD>" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --storage-size 32 \
  --version 15

az postgres flexible-server db create \
  --resource-group joiner-rg \
  --server-name joiner-db \
  --database-name joinerdb

# Allow the App Service (and your own IP, for the seed script) through -
# tighten this to specific addresses once you know them.
az postgres flexible-server firewall-rule create \
  --resource-group joiner-rg \
  --name joiner-db \
  --rule-name AllowAzureServices \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0
```

Your `DATABASE_URL` will be:

```
postgresql+psycopg2://joineradmin:<PASSWORD>@joiner-db.postgres.database.azure.com:5432/joinerdb?sslmode=require
```

## 4. Key Vault (so secrets never sit in plaintext anywhere)

This is what closes the biggest security gap in an early version of this
plan: the Graph app's client secret is stored in the database in plaintext
unless a Key Vault is configured. With one configured, `secrets_backend.py`
writes the real secret here instead and only a Key Vault secret *name*
ever touches Postgres.

```bash
az keyvault create \
  --resource-group joiner-rg \
  --name joiner-kv \
  --location uksouth
```

Give the App Service (created next) a system-assigned managed identity and
let *only that identity* read/write secrets - no credential of any kind
needs to be stored anywhere for this to work:

```bash
az webapp identity assign --resource-group joiner-rg --name joiner-backend
# copy the "principalId" this prints, then:
az keyvault set-policy \
  --name joiner-kv \
  --object-id <the principalId from above> \
  --secret-permissions get set
```

(Run the `az webapp create` command in the next step first if the app
doesn't exist yet, then come back and run the two commands above.)

## 5. App Service, running the container

```bash
az appservice plan create \
  --resource-group joiner-rg \
  --name joiner-plan \
  --is-linux --sku B1

az webapp create \
  --resource-group joiner-rg \
  --plan joiner-plan \
  --name joiner-backend \
  --deployment-container-image-name joinerregistry.azurecr.io/hybrid-joiner-backend:latest
```

Generate a real `DASHBOARD_TOKEN` (`openssl rand -base64 32`) and store it
in the vault too, rather than as a plain app setting:

```bash
az keyvault secret set --vault-name joiner-kv --name dashboard-token --value "<THE-GENERATED-TOKEN>"
```

Now wire the app settings - `DASHBOARD_TOKEN` uses App Service's native Key
Vault reference syntax (`@Microsoft.KeyVault(...)`), which App Service
resolves automatically at startup using the managed identity from step 4 -
no SDK code needed for this one, it's a platform feature:

```bash
az webapp config appsettings set \
  --resource-group joiner-rg \
  --name joiner-backend \
  --settings \
    DATABASE_URL="postgresql+psycopg2://joineradmin:<PASSWORD>@joiner-db.postgres.database.azure.com:5432/joinerdb?sslmode=require" \
    DASHBOARD_TOKEN="@Microsoft.KeyVault(SecretUri=https://joiner-kv.vault.azure.net/secrets/dashboard-token/)" \
    AZURE_KEY_VAULT_URL="https://joiner-kv.vault.azure.net/" \
    WEBSITES_PORT=8000

az webapp config set \
  --resource-group joiner-rg \
  --name joiner-backend \
  --always-on true
```

`AZURE_KEY_VAULT_URL` is what tells `secrets_backend.py` to actually use
Key Vault for the Graph client secret instead of the plaintext fallback -
without this setting the app still runs (useful for a throwaway test
deployment) but silently falls back to storing the secret directly in
Postgres, so don't skip it for anything real.

Your backend URL is `https://joiner-backend.azurewebsites.net`. Confirm
it's up:

```bash
curl https://joiner-backend.azurewebsites.net/health
```

## 6. Run the configure script against the deployed backend

Copy `config/config.example.yaml` to your own file (e.g.
`config/mycompany.yaml`) and fill in your real OUs, group names, domains
and license SKUs - that's the only file that should ever hold real
company data, and it's gitignored for exactly that reason. Then:

```bash
python3 scripts/configure_backend.py \
  --backend-url https://joiner-backend.azurewebsites.net \
  --dashboard-token "<THE-DASHBOARD_TOKEN-YOU-SET-ABOVE>" \
  --config config/mycompany.yaml \
  --aad-tenant-id <your real Entra tenant GUID> \
  --graph-client-id <the app registration's client id> \
  --graph-client-secret <the app registration's client secret>
```

Paste the printed connector key into `Connector-Agent.ps1`'s launch
parameters on the on-prem machine, and the client key into
`client/Client-Config.ps1` (copied from `Client-Config.example.ps1`)
alongside the same backend URL.

With `AZURE_KEY_VAULT_URL` set, the `--graph-client-secret` you pass here
never gets stored in Postgres - the script's `PUT /graph-credential` call
writes it straight to Key Vault and the database only ever sees the Key
Vault secret's name.

Need to check what's been issued, or revoke a key? `GET /keys` lists every
key (metadata only, never the value) and `POST /keys/{key_id}/revoke`
kills one immediately - useful if a laptop with the connector key on it
goes missing. `GET /audit-log` shows every config/credential/key change
with a timestamp.

This backend serves one customer per deployment - onboarding a second
company later means repeating this whole document (a new resource group,
a new Postgres, a new Key Vault, their own Entra app registration) for
their own copy of the same image, not adding them here.

## What's deliberately not covered here

- **The Entra app registration itself** - create it in the Azure Portal
  (App registrations -> New registration), add these **application**
  permissions under API permissions - `User.ReadWrite.All`,
  `Group.ReadWrite.All`, `Organization.Read.All` - and have a Global Admin
  grant admin consent. Deliberately **not** `Directory.ReadWrite.All`:
  nothing this backend does (updating a user's UsageLocation, managing
  group membership, assigning licenses) actually requires it - it's a much
  broader grant than this app needs, left over from the original
  interactive-delegated-permission scope list. That's a one-time manual
  step, not something to script.
- **TLS/custom domain** - App Service gives you `*.azurewebsites.net` with
  HTTPS out of the box, which is enough to start; a custom domain is a
  later, optional step.
- **Backups/monitoring/scaling** - a B1 plan and Burstable Postgres tier
  are starting points for a pilot with one company, not a sized-for-load
  production setup. Revisit before this carries real production traffic.
