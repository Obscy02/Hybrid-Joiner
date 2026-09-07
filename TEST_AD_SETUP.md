# Setting up a throwaway test AD forest in Azure

This gives you a real Active Directory domain, synced to your E5 trial
Entra tenant via Azure AD Connect, so `Connector-Agent.ps1` has something
real to run against instead of `respx` mocks. One VM does double duty as
both the domain controller and the connector host — fine for a throwaway
test lab, not something to carry into a real deployment (there, keep the
connector off your DC, per `GETTING_STARTED.md`'s own reasoning).

**Cost**: B2s base compute is about $0.047/hour in UK South - Windows
Server licensing adds a surcharge on top of that, and exact rates vary by
region and change over time. **Before creating the VM, check the real
number**:
[Azure's pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/),
add a B2s Windows VM in your region, and read the actual hourly figure it
shows you. Whatever it is, a few days of on-and-off testing is cheap - this
is not the expensive part of any of this. **Deallocate
the VM (not just shut down Windows) whenever you're not actively testing**
— Azure only stops charging for compute once a VM is deallocated, and the
OS disk keeps billing a small amount even while deallocated, so delete the
whole resource group when you're done for good.

**Don't reuse a domain name you might ever use for real** — pick something
obviously throwaway, e.g. `testlab.local`, not anything resembling your
actual domain.

---

## Step 1 — Provision the VM

Run from your own machine (needs the `az` CLI, already used in
`DEPLOY_AZURE.md`):

```bash
az group create --name joiner-test-rg --location uksouth

az network vnet create \
  --resource-group joiner-test-rg \
  --name test-vnet \
  --subnet-name test-subnet

# Find your own public IP - RDP gets locked to ONLY this address, never
# opened to the whole internet (an open RDP port gets brute-forced within
# minutes of existing).
MY_IP=$(curl -s ifconfig.me)
echo "Your IP: $MY_IP"

az network nsg create --resource-group joiner-test-rg --name test-nsg

az network nsg rule create \
  --resource-group joiner-test-rg \
  --nsg-name test-nsg \
  --name AllowRDPFromMe \
  --priority 100 \
  --source-address-prefixes "$MY_IP/32" \
  --destination-port-ranges 3389 \
  --access Allow --protocol Tcp

az vm create \
  --resource-group joiner-test-rg \
  --name test-dc01 \
  --image Win2022Datacenter \
  --size Standard_B2s \
  --vnet-name test-vnet --subnet test-subnet \
  --nsg test-nsg \
  --admin-username azureuser \
  --admin-password "<CHOOSE-A-REAL-PASSWORD-12+CHARS-MIXED-CASE-NUMBERS>"
```

If `Win2022Datacenter` gives an "image not found" error (image aliases
occasionally get renamed), find the current one with:
```bash
az vm image list --all --publisher MicrosoftWindowsServer -o table | grep 2022
```
and substitute whichever `Urn` or alias comes back for `Win2022Datacenter`
in the `--image` flag above.

**Check it worked**: the last command prints a `publicIpAddress` - note it
down.

## Step 2 — Connect and promote it to a new AD forest

RDP into `<the publicIpAddress from step 1>` using Windows' Remote Desktop
Connection, username `azureuser`, the password you set above.

Once connected, open PowerShell **as Administrator** on the VM itself and
run:

```powershell
Install-WindowsFeature -Name AD-Domain-Services -IncludeManagementTools

Install-ADDSForest `
    -DomainName "testlab.local" `
    -DomainNetbiosName "TESTLAB" `
    -InstallDns `
    -SafeModeAdministratorPassword (ConvertTo-SecureString "<ANOTHER-REAL-PASSWORD>" -AsPlainText -Force) `
    -Force
```

This reboots the VM automatically. Wait a minute, then RDP back in - this
time the username needs the domain prefix: `TESTLAB\azureuser`.

**Check it worked**: open PowerShell and run `Get-ADDomain` - it should
print details about `testlab.local`, not an error.

## Step 3 — Install Azure AD Connect

Still on the VM (it's now both your DC and, shortly, your connector host):

1. Download Azure AD Connect from
   [microsoft.com/download/details.aspx?id=47594](https://www.microsoft.com/download/details.aspx?id=47594)
   directly on the VM (open Edge inside the RDP session).
2. Run the installer. Choose **Customize** (not Express) so you can see
   what it's doing.
3. Sign in with your E5 trial tenant's Global Admin account when prompted
   for the Azure AD (Entra) side.
4. Sign in with `TESTLAB\azureuser` (an enterprise admin in this new
   throwaway forest) for the on-prem AD side.
5. Accept the defaults for what to sync (all of `testlab.local` is fine for
   a single-domain test forest).
6. Let it finish and do an initial sync.

**Check it worked**: in the Entra portal for your E5 trial tenant, go to
**Users** - you should see a new user matching whatever's in
`testlab.local` (at minimum, the built-in Administrator or whatever test
users you create in step 4 below, once they've synced).

## Step 4 — Create one test user in AD to clone from

The hybrid joiner tool's whole design clones settings from a "similar
colleague" - it needs at least one real account to clone from.

```powershell
New-ADUser -Name "Test Colleague" -SamAccountName "testcolleague" `
    -UserPrincipalName "testcolleague@testlab.local" `
    -Path "CN=Users,DC=testlab,DC=local" `
    -AccountPassword (ConvertTo-SecureString "SomeRealPassword123!" -AsPlainText -Force) `
    -Enabled $true -PasswordNeverExpires $true `
    -City "Test City" -OfficePhone "0000 000 0000"
```

Wait for the next Azure AD Connect sync cycle (default every 30 minutes,
or force one immediately from the VM: `Start-ADSyncSyncCycle -PolicyType Delta`).

**Check it worked**: `Test Colleague` shows up in the Entra portal's user
list.

## Step 5 — Point the connector at this test setup

You now have everything `GETTING_STARTED.md` assumes, just pointed at a
test tenant instead of a real customer's:

- **Entra app registration**: follow `GETTING_STARTED.md` Step 1, but in
  your E5 trial tenant.
- **Backend**: follow Step 2 as written (or reuse a backend you already
  deployed - just give it this test tenant's Graph credential via
  `PUT /graph-credential`).
- **Config**: copy `config/config.example.yaml` to a throwaway file with
  `site_ou_fallback` pointing at `CN=Users,DC=testlab,DC=local`,
  `company_domain_fallback` mapping some test company name to
  `testlab.local`, and a couple of `cloud_group_rules` pointing at real
  Entra groups you create in the trial tenant for this purpose. Load it
  with `scripts/configure_backend.py --config <that file> ...`.
- **Connector**: since this VM is domain-joined and already has the AD
  module (it's a DC), copy `connector/Connector-Agent.ps1` and
  `Register-ConnectorTask.ps1` onto it and run the same registration step
  from `GETTING_STARTED.md` Step 4.
- **Test it**: run a joiner through with `"Test Colleague"` as the similar
  colleague, watch it create a real AD account, sync, and get real
  Entra groups/a real E5 trial license assigned.

## When you're done

```bash
az group delete --resource-group joiner-test-rg --yes --no-wait
```

This deletes the VM, its disk, and the network in one step - nothing left
billing.
