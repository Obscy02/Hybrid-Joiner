<#
Generic on-prem connector agent.

Installed inside the customer's network (as a scheduled task or service)
with the ActiveDirectory module available. Makes only OUTBOUND calls to
the backend - polls for pending joiner jobs, does the on-prem AD work
locally where it already has line-of-sight to AD, and reports back. No
inbound firewall rule needed on the customer's side, the same shape Entra
Connect itself uses.

Every OU/domain/group/phone value it needs comes from the config fetched
from the backend at the start of each job - nothing about the specific
customer's environment is hardcoded in this file. This is a generic
re-implementation of the "clone from a similar colleague, fall back to
config" pattern, not a copy of any specific customer's onboarding script -
a second customer runs this exact same file against their own deployment.

Parameters:
  -BackendUrl     e.g. https://your-backend-host
  -ConnectorKey   API key issued via POST /keys?role=connector
  -PollSeconds    how often to check for new jobs (default 30)
#>
param(
    [Parameter(Mandatory = $true)][string]$BackendUrl,
    [Parameter(Mandatory = $true)][string]$ConnectorKey,
    [int]$PollSeconds = 30
)

Import-Module ActiveDirectory -ErrorAction Stop

$Headers = @{ Authorization = "Bearer $ConnectorKey" }

function Invoke-BackendApi {
    param([string]$Method, [string]$Path, [hashtable]$Body)
    $uri = "$BackendUrl$Path"
    if ($Body) {
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $Headers -Body ($Body | ConvertTo-Json -Depth 10) -ContentType "application/json"
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $Headers
}

function New-RandomPassword {
    <#
    RNGCryptoServiceProvider rather than Get-Random - the latter isn't
    cryptographically secure and shouldn't generate anything used as a
    real account credential, even a temporary one.
    #>
    $bytes = New-Object byte[] 16
    $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::new()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%'
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function ConvertTo-FilterSafe {
    param([string]$Value)
    if (-not $Value) { return $Value }
    return $Value.Replace("'", "''")
}

function Resolve-SiteAndDomain {
    <#
    Mirrors the "clone from a similar colleague first, fall back to
    config" approach: prefer deriving OU/domain from a real existing
    account over trusting free-text form fields, and only fall back to the
    config lookup tables when that colleague can't be found.
    #>
    param($JoinerFields, $Config)

    $colleague = $null
    if ($JoinerFields.similar_colleague) {
        $safeName = ConvertTo-FilterSafe $JoinerFields.similar_colleague
        $matches = @(Get-ADUser -Filter "DisplayName -eq '$safeName'" -Properties MemberOf, UserPrincipalName, OfficePhone, StreetAddress, City, PostalCode, State, Country)
        if ($matches.Count -gt 0) { $colleague = $matches[0] }
    }

    $targetOU = $null
    $upnSuffix = $null

    if ($colleague) {
        $targetOU = ($colleague.DistinguishedName -split '(?<!\\),', 2)[1]
        if ($colleague.UserPrincipalName -match '@(.+)$') { $upnSuffix = $Matches[1] }
    }

    if (-not $targetOU -and $Config.site_ou_fallback.PSObject.Properties[$JoinerFields.site]) {
        $targetOU = $Config.site_ou_fallback.($JoinerFields.site)
    }
    if (-not $upnSuffix -and $Config.company_domain_fallback.PSObject.Properties[$JoinerFields.company]) {
        $upnSuffix = $Config.company_domain_fallback.($JoinerFields.company)
    }

    return [PSCustomObject]@{
        Colleague = $colleague
        TargetOU  = $targetOU
        UpnSuffix = $upnSuffix
    }
}

function Invoke-JoinerJob {
    param($Job, $Config)

    $log = { param($msg) Invoke-BackendApi -Method Post -Path "/connector/jobs/$($Job.id)/log" -Body @{ line = $msg } | Out-Null }

    & $log "[i] Claimed job for $($Job.joiner_fields.full_name)."

    $resolved = Resolve-SiteAndDomain -JoinerFields $Job.joiner_fields -Config $Config
    if (-not $resolved.TargetOU -or -not $resolved.UpnSuffix) {
        Invoke-BackendApi -Method Post -Path "/connector/jobs/$($Job.id)/fail" -Body @{
            error_message = "Could not resolve target OU or email domain - similar colleague not found and no config fallback for this site/company."
        } | Out-Null
        return
    }

    $firstName, $lastName = $Job.joiner_fields.full_name -split ' ', 2
    $samAccountName = $Job.joiner_fields.requested_logon_name
    $upn = "$firstName.$lastName@$($resolved.UpnSuffix)" -replace '\s', ''

    $existing = @(Get-ADUser -Filter "UserPrincipalName -eq '$(ConvertTo-FilterSafe $upn)' -or SamAccountName -eq '$(ConvertTo-FilterSafe $samAccountName)'")
    if ($existing.Count -gt 0) {
        Invoke-BackendApi -Method Post -Path "/connector/jobs/$($Job.id)/fail" -Body @{
            error_message = "An account with this logon name or email already exists - aborted before creating anything."
        } | Out-Null
        return
    }

    $password = New-RandomPassword
    $newUserParams = @{
        Name                  = $Job.joiner_fields.full_name
        SamAccountName        = $samAccountName
        UserPrincipalName     = $upn
        EmailAddress          = $upn
        GivenName             = $firstName
        Surname               = $lastName
        Path                  = $resolved.TargetOU
        AccountPassword       = (ConvertTo-SecureString $password -AsPlainText -Force)
        Enabled               = $true
        ChangePasswordAtLogon = $true
    }
    if ($resolved.Colleague) {
        if ($resolved.Colleague.OfficePhone) { $newUserParams.OfficePhone = $resolved.Colleague.OfficePhone }
        if ($resolved.Colleague.StreetAddress) { $newUserParams.StreetAddress = $resolved.Colleague.StreetAddress }
        if ($resolved.Colleague.City) { $newUserParams.City = $resolved.Colleague.City }
        if ($resolved.Colleague.PostalCode) { $newUserParams.PostalCode = $resolved.Colleague.PostalCode }
        if ($resolved.Colleague.State) { $newUserParams.State = $resolved.Colleague.State }
        if ($resolved.Colleague.Country) { $newUserParams.Country = $resolved.Colleague.Country }
    }

    try {
        New-ADUser @newUserParams -ErrorAction Stop
        & $log "[OK] AD user created successfully."
        & $log "[i] Temporary password (share securely): $password"
    } catch {
        Invoke-BackendApi -Method Post -Path "/connector/jobs/$($Job.id)/fail" -Body @{ error_message = "New-ADUser failed: $($_.Exception.Message)" } | Out-Null
        return
    }

    if ($resolved.Colleague) {
        $excluded = $Config.excluded_group_patterns
        $groupsToClone = $resolved.Colleague.MemberOf | Where-Object {
            $groupName = ($_ -split ',')[0] -replace '^CN=', ''
            -not ($excluded | Where-Object { $groupName -like $_ }) -and $groupName -ne $Config.line_manager_group
        }
        foreach ($groupDn in $groupsToClone) {
            try { Add-ADGroupMember -Identity $groupDn -Members $samAccountName -ErrorAction Stop }
            catch { & $log "[WARN] Could not clone group $groupDn`: $($_.Exception.Message)" }
        }
        & $log "[OK] Cloned $($groupsToClone.Count) on-premises group(s) from similar colleague."
    }

    if ($Config.line_manager_group -and ($Job.joiner_fields.is_line_manager -match 'Yes')) {
        try { Add-ADGroupMember -Identity $Config.line_manager_group -Members $samAccountName -ErrorAction Stop }
        catch { & $log "[WARN] Could not add to line manager group: $($_.Exception.Message)" }
    }

    if ($Config.ad_sync_server) {
        try {
            Invoke-Command -ComputerName $Config.ad_sync_server -ScriptBlock { Start-ADSyncSyncCycle -PolicyType Delta } -ErrorAction Stop | Out-Null
            & $log "[OK] Triggered delta AD sync."
        } catch {
            & $log "[WARN] Could not trigger AD sync remotely - it will still pick this up on its normal schedule."
        }
    }

    Invoke-BackendApi -Method Post -Path "/connector/jobs/$($Job.id)/onprem-complete" -Body @{
        ad_samaccountname = $samAccountName
        ad_upn            = $upn
    } | Out-Null
    & $log "[OK] On-prem steps complete - handed off to backend for group/license assignment."
}

Write-Host "Connector agent started. Polling $BackendUrl every $PollSeconds seconds..."
while ($true) {
    try {
        $config = Invoke-BackendApi -Method Get -Path "/connector/config"
        $job = Invoke-BackendApi -Method Post -Path "/connector/jobs/claim"
        if ($job) {
            Invoke-JoinerJob -Job $job -Config $config
        }
    } catch {
        Write-Warning "Poll cycle failed: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $PollSeconds
}
