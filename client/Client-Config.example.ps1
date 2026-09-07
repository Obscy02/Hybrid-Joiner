<#
Deployment-specific settings for Joiner-Client.ps1. Dot-sourced by that
script - the only file an admin needs to edit after the backend is
deployed and scripts/configure_backend.py has been run once.

Copy this file to "Client-Config.ps1" (same folder) and fill in the two
values below - that copy is gitignored so a real backend URL and API key
never end up committed.
#>

$BackendUrl = "https://REPLACE-WITH-YOUR-DEPLOYED-BACKEND-HOST"
$ClientApiKey = "REPLACE-WITH-THE-CLIENT-KEY-FROM-configure_backend.py"
