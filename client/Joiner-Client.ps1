<#
Thin client for the admin team - drag & drop the joiner form, review the
suggested logon name, click Create User, watch the status box. It does
none of the AD or Graph work itself: it uploads the joiner form to the
backend and polls for status while the connector (running elsewhere, on a
domain-joined machine) and the backend's own background task do the
actual provisioning.

Nothing in this file is deployment-specific except what's dot-sourced from
Client-Config.ps1 (backend URL, API key) - every deployment of this
project runs this same script unchanged, same as Connector-Agent.ps1 on
the on-prem side.

No ActiveDirectory/Microsoft.Graph modules needed on this machine at all -
that's the point of moving the actual work server-side.
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Net.Http

# Without these, WinForms renders with classic (pre-XP) control styling and
# scales badly on anything but 100% display scaling - both read as
# distinctly "not modern" on a current Windows 11 laptop. Must happen
# before any Form/control is created.
[System.Windows.Forms.Application]::EnableVisualStyles()
try {
    [System.Windows.Forms.Application]::SetHighDpiMode([System.Windows.Forms.HighDpiMode]::SystemAware) | Out-Null
} catch {
    # SetHighDpiMode needs .NET Framework 4.7+ - silently skip on anything
    # older rather than fail the whole tool over a cosmetic improvement.
}

$ConfigPath = Join-Path $PSScriptRoot "Client-Config.ps1"
try {
    . $ConfigPath
} catch {
    [System.Windows.Forms.MessageBox]::Show(
        "Could not load Client-Config.ps1 from this folder:`n`n$($_.Exception.Message)`n`nMake sure it's in the same folder as this tool.",
        "New Joiner Tool - Setup Error",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    exit 1
}

$BackendUrl = $BackendUrl.TrimEnd('/')
$ConfigLooksUnset = (
    -not $BackendUrl -or -not $ClientApiKey -or
    $BackendUrl -like "*REPLACE-WITH*" -or $ClientApiKey -like "*REPLACE-WITH*"
)
if ($ConfigLooksUnset) {
    [System.Windows.Forms.MessageBox]::Show(
        "Client-Config.ps1 still has its placeholder values.`n`nEdit `$BackendUrl and `$ClientApiKey in that file (in this same folder) before running this tool.",
        "New Joiner Tool - Setup Error",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    exit 1
}

#region Helpers
function Get-SuggestedSamAccountName {
    # FirstName + initial of each hyphen-segment of the surname, e.g.
    # Evan Wellard-McMillan -> EVANWM. Always shown editable - this is a
    # suggestion, not a verified logon name.
    param([string]$FirstName, [string]$LastName)
    if (-not $FirstName -or -not $LastName) { return "" }
    if ($LastName -match '-') {
        $suffix = (($LastName -split '-') | ForEach-Object { $_.Substring(0, 1) }) -join ''
    } else {
        $suffix = $LastName.Substring(0, 1)
    }
    return ($FirstName + $suffix).ToUpper()
}

$script:HttpClient = [System.Net.Http.HttpClient]::new()
$script:HttpClient.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $ClientApiKey)

function Invoke-BackendUpload {
    # Multipart upload via HttpClient rather than Invoke-RestMethod -Form,
    # since -Form needs PowerShell 6.1+ and this has to work on the Windows
    # PowerShell 5.1 most admin machines still ship with.
    param([string]$Path, [string]$RouteSuffix)
    $content = [System.Net.Http.MultipartFormDataContent]::new()
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $fileContent = [System.Net.Http.ByteArrayContent]::new($bytes)
    $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
    $content.Add($fileContent, "file", [System.IO.Path]::GetFileName($Path))

    $uri = "$BackendUrl/$RouteSuffix"
    $response = $script:HttpClient.PostAsync($uri, $content).GetAwaiter().GetResult()
    $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
        throw "Backend returned $($response.StatusCode): $body"
    }
    return $body | ConvertFrom-Json
}

function Invoke-BackendJson {
    param([string]$Method, [string]$RouteSuffix, $Body)
    $uri = "$BackendUrl/$RouteSuffix"
    $headers = @{ Authorization = "Bearer $ClientApiKey" }
    if ($null -ne $Body) {
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -Body ($Body | ConvertTo-Json -Depth 10) -ContentType "application/json"
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers
}
#endregion

#region UI theme
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class NativeMethods {
    [DllImport("dwmapi.dll")]
    public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int attrValue, int attrSize);
}
"@

# Follows the signed-in user's Windows theme, the same way any current
# native Windows 11 app does - a light-only tool looks dated to anyone
# running dark mode, which is a common preference, not an edge case.
function Test-WindowsDarkMode {
    try {
        $value = Get-ItemPropertyValue -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" -Name "AppsUseLightTheme" -ErrorAction Stop
        return ($value -eq 0)
    } catch {
        return $false  # registry value missing (older Windows) - default to light
    }
}
$IsDarkMode = Test-WindowsDarkMode

if ($IsDarkMode) {
    $ColorWindowBg      = [System.Drawing.Color]::FromArgb(32, 32, 32)
    $ColorCardBg        = [System.Drawing.Color]::FromArgb(44, 44, 44)
    $ColorBorder        = [System.Drawing.Color]::FromArgb(64, 64, 64)
    $ColorAccent        = [System.Drawing.Color]::FromArgb(76, 194, 255)
    $ColorAccentHover   = [System.Drawing.Color]::FromArgb(56, 174, 235)
    $ColorTextPrimary   = [System.Drawing.Color]::FromArgb(255, 255, 255)
    $ColorTextSecondary = [System.Drawing.Color]::FromArgb(200, 200, 200)
    $ColorLogBg         = [System.Drawing.Color]::FromArgb(20, 20, 20)
    $ColorHoverBg       = [System.Drawing.Color]::FromArgb(58, 58, 58)
    $ColorButtonText    = [System.Drawing.Color]::FromArgb(20, 20, 20)
} else {
    $ColorWindowBg      = [System.Drawing.Color]::FromArgb(243, 243, 243)
    $ColorCardBg        = [System.Drawing.Color]::White
    $ColorBorder        = [System.Drawing.Color]::FromArgb(224, 224, 224)
    $ColorAccent        = [System.Drawing.Color]::FromArgb(0, 120, 212)
    $ColorAccentHover   = [System.Drawing.Color]::FromArgb(16, 110, 190)
    $ColorTextPrimary   = [System.Drawing.Color]::FromArgb(32, 32, 32)
    $ColorTextSecondary = [System.Drawing.Color]::FromArgb(96, 94, 92)
    $ColorLogBg         = [System.Drawing.Color]::FromArgb(30, 30, 30)
    $ColorHoverBg       = [System.Drawing.Color]::FromArgb(240, 240, 240)
    $ColorButtonText    = [System.Drawing.Color]::White
}

$FontTitle    = New-Object System.Drawing.Font("Segoe UI", 17, [System.Drawing.FontStyle]::Bold)
$FontSubtitle = New-Object System.Drawing.Font("Segoe UI", 9.5)
$FontSection  = New-Object System.Drawing.Font("Segoe UI", 8.5, [System.Drawing.FontStyle]::Bold)
$FontBody     = New-Object System.Drawing.Font("Segoe UI", 10)
$FontCaption  = New-Object System.Drawing.Font("Segoe UI", 8.5)
$FontButton   = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$FontMono     = New-Object System.Drawing.Font("Consolas", 9.5)

$IconFolder = [char]::ConvertFromUtf32(0x1F4C1)
$IconCheck  = [char]0x2705

function Set-RoundedRegion {
    param($Control, [int]$Radius = 8)
    $w = $Control.Width; $h = $Control.Height; $d = $Radius * 2
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc(0, 0, $d, $d, 180, 90)
    $path.AddArc($w - $d, 0, $d, $d, 270, 90)
    $path.AddArc($w - $d, $h - $d, $d, $d, 0, 90)
    $path.AddArc(0, $h - $d, $d, $d, 90, 90)
    $path.CloseAllFigures()
    $Control.Region = New-Object System.Drawing.Region($path)
}

function New-SectionLabel {
    param([string]$Text, [int]$X, [int]$Y)
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = $Text.ToUpper()
    $lbl.Font = $FontSection
    $lbl.ForeColor = $ColorAccent
    $lbl.AutoSize = $true
    $lbl.Location = New-Object System.Drawing.Point($X, $Y)
    return $lbl
}
#endregion

#region Build form
$Form = New-Object System.Windows.Forms.Form
$Form.Text = "Hybrid Joiner Tool"
$Form.Size = New-Object System.Drawing.Size(600, 720)
$Form.StartPosition = "CenterScreen"
$Form.FormBorderStyle = 'FixedDialog'
$Form.MaximizeBox = $false
$Form.BackColor = $ColorWindowBg
$Form.Font = $FontBody

$Form.Add_Shown({
    try {
        $roundedPref = 2  # DWMWCP_ROUND - Windows 11 only; silently no-ops on Windows 10
        [NativeMethods]::DwmSetWindowAttribute($Form.Handle, 33, [ref]$roundedPref, 4) | Out-Null
        # Attribute 20 = DWMWA_USE_IMMERSIVE_DARK_MODE - makes the title
        # bar itself dark too, so it doesn't stay a jarring white bar on
        # top of an otherwise dark-themed window.
        $darkPref = [int]$IsDarkMode
        [NativeMethods]::DwmSetWindowAttribute($Form.Handle, 20, [ref]$darkPref, 4) | Out-Null
    } catch {}
})

$TitleLabel = New-Object System.Windows.Forms.Label
$TitleLabel.Text = "New Joiner Setup"
$TitleLabel.Font = $FontTitle
$TitleLabel.ForeColor = $ColorTextPrimary
$TitleLabel.AutoSize = $true
$TitleLabel.Location = New-Object System.Drawing.Point(24, 24)
$Form.Controls.Add($TitleLabel)

$SubtitleLabel = New-Object System.Windows.Forms.Label
$SubtitleLabel.Text = "Provision a hybrid Active Directory + Microsoft 365 account"
$SubtitleLabel.Font = $FontSubtitle
$SubtitleLabel.ForeColor = $ColorTextSecondary
$SubtitleLabel.AutoSize = $true
$SubtitleLabel.Location = New-Object System.Drawing.Point(26, 60)
$Form.Controls.Add($SubtitleLabel)

$Form.Controls.Add((New-SectionLabel -Text "Step 1 - New Joiner File" -X 24 -Y 96))

$DropZone = New-Object System.Windows.Forms.Label
$DropZone.Text = "$IconFolder`n`nDrag && drop the New Joiner Excel file here`n(or click Browse)"
$DropZone.TextAlign = 'MiddleCenter'
$DropZone.Font = $FontBody
$DropZone.ForeColor = $ColorTextSecondary
$DropZone.BackColor = $ColorCardBg
$DropZone.Location = New-Object System.Drawing.Point(24, 116)
$DropZone.Size = New-Object System.Drawing.Size(552, 110)
$DropZone.AllowDrop = $true
$DropZone.Add_Paint({
    param($sender, $e)
    $pen = New-Object System.Drawing.Pen($ColorAccent, 1.5)
    $pen.DashStyle = [System.Drawing.Drawing2D.DashStyle]::Dash
    $rectWidth = $sender.Width - 3
    $rectHeight = $sender.Height - 3
    $rect = [System.Drawing.Rectangle]::new(1, 1, $rectWidth, $rectHeight)
    $e.Graphics.DrawRectangle($pen, $rect)
    $pen.Dispose()
})
Set-RoundedRegion -Control $DropZone -Radius 10
$Form.Controls.Add($DropZone)

$script:ExcelPath = $null
$script:PreviewedFields = $null
$script:LogFilePath = $null
$script:PollTimer = $null
$script:ShownLogCount = 0

function Set-SelectedExcelFile {
    param([string]$Path)
    $script:ExcelPath = $Path
    $DropZone.Text = "$IconCheck Selected: $(Split-Path -Leaf $Path)`n`nReading form..."
    [System.Windows.Forms.Application]::DoEvents()
    try {
        $Fields = Invoke-BackendUpload -Path $Path -RouteSuffix "preview-excel"
        $script:PreviewedFields = $Fields
        if ($Fields.full_name) {
            $Parts = $Fields.full_name -split ' '
            $UserTextBox.Text = Get-SuggestedSamAccountName -FirstName $Parts[0] -LastName $Parts[-1]
        }
        $DropZone.Text = "$IconCheck Selected: $(Split-Path -Leaf $Path)`n`nDetected joiner: $($Fields.full_name)"
    } catch {
        $script:PreviewedFields = $null
        $DropZone.Text = "$IconCheck Selected: $(Split-Path -Leaf $Path)`n`n(Could not preview: $($_.Exception.Message))"
    }
}

$DropZone.Add_DragEnter({
    param($sender, $e)
    if ($e.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) {
        $e.Effect = [System.Windows.Forms.DragDropEffects]::Copy
    }
})

$DropZone.Add_DragDrop({
    param($sender, $e)
    $files = $e.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop)
    if ($files -and $files.Count -gt 0) {
        Set-SelectedExcelFile -Path $files[0]
    }
})

$BrowseButton = New-Object System.Windows.Forms.Button
$BrowseButton.Text = "Browse files..."
$BrowseButton.Font = $FontCaption
$BrowseButton.ForeColor = $ColorAccent
$BrowseButton.BackColor = $ColorCardBg
$BrowseButton.FlatStyle = 'Flat'
$BrowseButton.FlatAppearance.BorderColor = $ColorBorder
$BrowseButton.FlatAppearance.BorderSize = 1
$BrowseButton.FlatAppearance.MouseOverBackColor = $ColorHoverBg
$BrowseButton.Location = New-Object System.Drawing.Point(24, 232)
$BrowseButton.Size = New-Object System.Drawing.Size(120, 26)
Set-RoundedRegion -Control $BrowseButton -Radius 6
$Form.Controls.Add($BrowseButton)

$BrowseButton.Add_Click({
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Filter = "Excel Files (*.xlsx)|*.xlsx"
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        Set-SelectedExcelFile -Path $dlg.FileName
    }
})

$Form.Controls.Add((New-SectionLabel -Text "Step 2 - Logon Name" -X 24 -Y 276))

$UserTextBox = New-Object System.Windows.Forms.TextBox
$UserTextBox.Font = $FontBody
$UserTextBox.BorderStyle = 'FixedSingle'
$UserTextBox.Location = New-Object System.Drawing.Point(24, 296)
$UserTextBox.Size = New-Object System.Drawing.Size(552, 28)
$Form.Controls.Add($UserTextBox)

$UserCaption = New-Object System.Windows.Forms.Label
$UserCaption.Text = "Auto-suggested once you select a file - verify against your HR system before continuing."
$UserCaption.Font = $FontCaption
$UserCaption.ForeColor = $ColorTextSecondary
$UserCaption.AutoSize = $true
$UserCaption.Location = New-Object System.Drawing.Point(24, 330)
$Form.Controls.Add($UserCaption)

$SubmitButton = New-Object System.Windows.Forms.Button
$SubmitButton.Text = "Create User"
$SubmitButton.Font = $FontButton
$SubmitButton.ForeColor = $ColorButtonText
$SubmitButton.BackColor = $ColorAccent
$SubmitButton.FlatStyle = 'Flat'
$SubmitButton.FlatAppearance.BorderSize = 0
$SubmitButton.FlatAppearance.MouseOverBackColor = $ColorAccentHover
$SubmitButton.Location = New-Object System.Drawing.Point(24, 364)
$SubmitButton.Size = New-Object System.Drawing.Size(180, 42)
Set-RoundedRegion -Control $SubmitButton -Radius 8
$Form.Controls.Add($SubmitButton)
$Form.AcceptButton = $SubmitButton  # Enter anywhere in the window submits, standard Windows dialog behavior

$ResetButton = New-Object System.Windows.Forms.Button
$ResetButton.Text = "Reset"
$ResetButton.Font = $FontCaption
$ResetButton.ForeColor = $ColorAccent
$ResetButton.BackColor = $ColorCardBg
$ResetButton.FlatStyle = 'Flat'
$ResetButton.FlatAppearance.BorderColor = $ColorBorder
$ResetButton.FlatAppearance.BorderSize = 1
$ResetButton.FlatAppearance.MouseOverBackColor = $ColorHoverBg
$ResetButton.Location = New-Object System.Drawing.Point(216, 364)
$ResetButton.Size = New-Object System.Drawing.Size(120, 42)
Set-RoundedRegion -Control $ResetButton -Radius 8
$Form.Controls.Add($ResetButton)

# A slim, unobtrusive "something is happening" indicator for the up-to-
# 15-minute wait - visible only while a job is actively being polled, so
# the window doesn't rely on periodic log lines alone to feel alive.
$ProgressBar = New-Object System.Windows.Forms.ProgressBar
$ProgressBar.Style = 'Marquee'
$ProgressBar.MarqueeAnimationSpeed = 30
$ProgressBar.Location = New-Object System.Drawing.Point(24, 412)
$ProgressBar.Size = New-Object System.Drawing.Size(552, 4)
$ProgressBar.Visible = $false
$Form.Controls.Add($ProgressBar)

$Form.Controls.Add((New-SectionLabel -Text "Status" -X 24 -Y 426))

$LogBox = New-Object System.Windows.Forms.RichTextBox
$LogBox.Location = New-Object System.Drawing.Point(24, 446)
$LogBox.Size = New-Object System.Drawing.Size(552, 210)
$LogBox.ReadOnly = $true
$LogBox.BorderStyle = 'None'
$LogBox.BackColor = $ColorLogBg
$LogBox.ForeColor = [System.Drawing.Color]::White
$LogBox.Font = $FontMono
Set-RoundedRegion -Control $LogBox -Radius 10
$Form.Controls.Add($LogBox)

function Reset-ClientState {
    $script:ExcelPath = $null
    $script:PreviewedFields = $null
    $script:LogFilePath = $null
    $script:ShownLogCount = 0
    if ($script:PollTimer) { $script:PollTimer.Stop() }
    $ProgressBar.Visible = $false
    $DropZone.Text = "$IconFolder`n`nDrag && drop the New Joiner Excel file here`n(or click Browse)"
    $UserTextBox.Text = ""
    $LogBox.Clear()
}

$ResetButton.Add_Click({ Reset-ClientState })

function Write-Log {
    param([string]$Message, [string]$Color = "White")
    $LogBox.SelectionStart = $LogBox.TextLength
    $LogBox.SelectionLength = 0
    $LogBox.SelectionColor = [System.Drawing.Color]::$Color
    $LogBox.AppendText("$Message`n")
    $LogBox.ScrollToCaret()
    if ($script:LogFilePath) {
        Add-Content -Path $script:LogFilePath -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $Message" -ErrorAction SilentlyContinue
    }
}

function Get-LogColor {
    # Mirrors the prefix convention the backend/connector write into
    # log_lines, so this client colors them consistently. Plain
    # .Contains(), not -like - "[" and "]" are wildcard character-class
    # syntax to -like, so "*[FAIL]*" would match almost any line
    # containing an F, A, I or L rather than the literal substring
    # "[FAIL]".
    param([string]$Line)
    if ($Line.Contains("[FAIL]"))                                  { return "Red" }
    if ($Line.Contains("[WARN]") -or $Line.Contains("[!]"))        { return "Orange" }
    if ($Line.Contains("[OK]"))                                    { return "LightGreen" }
    if ($Line.Contains("[SKIP]"))                                  { return "Gray" }
    if ($Line.Contains("[i]"))                                     { return "Yellow" }
    return "White"
}
#endregion

#region Submit logic
$SubmitButton.Add_Click({
    $SubmitButton.Enabled = $false
    $ResetButton.Enabled = $false
    $LogBox.Clear()
    $script:ShownLogCount = 0

    if (-not $script:ExcelPath -or -not (Test-Path $script:ExcelPath)) {
        Write-Log "[!] Please select a valid Excel file." "Red"
        $SubmitButton.Enabled = $true; $ResetButton.Enabled = $true
        return
    }
    if (-not $script:PreviewedFields) {
        Write-Log "[!] Could not read this form - re-select the file and check the error shown in the drop zone." "Red"
        $SubmitButton.Enabled = $true; $ResetButton.Enabled = $true
        return
    }
    $sAMAccountName = $UserTextBox.Text.Trim()
    if (-not $sAMAccountName) {
        Write-Log "[!] Please enter the verified logon name." "Red"
        $SubmitButton.Enabled = $true; $ResetButton.Enabled = $true
        return
    }

    $LogDir = Join-Path $env:APPDATA "JoinerToolLogs"
    New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction SilentlyContinue | Out-Null
    $script:LogFilePath = Join-Path $LogDir "Joiner_Log_${sAMAccountName}_$(Get-Date -Format 'yyyyMMdd_HHmmss').log"

    Write-Log "[i] Submitting job for $($script:PreviewedFields.full_name)..." "Yellow"

    $JoinerFields = @{}
    $script:PreviewedFields.PSObject.Properties | ForEach-Object { $JoinerFields[$_.Name] = $_.Value }
    $JoinerFields.requested_logon_name = $sAMAccountName

    try {
        $Job = Invoke-BackendJson -Method Post -RouteSuffix "jobs" -Body $JoinerFields
    } catch {
        Write-Log "[FAIL] Could not submit job to backend: $($_.Exception.Message)" "Red"
        $SubmitButton.Enabled = $true; $ResetButton.Enabled = $true
        return
    }

    Write-Log "[i] Job #$($Job.id) queued - waiting for the on-prem agent to pick it up." "Yellow"
    Write-Log "[i] This can take up to 15 minutes (mostly Entra sync) - the window stays usable the whole time." "Gray"

    $script:PollTimer = New-Object System.Windows.Forms.Timer
    $script:PollTimer.Interval = 4000
    $script:PollTimer.Add_Tick({
        try {
            $Current = Invoke-BackendJson -Method Get -RouteSuffix "jobs/$($Job.id)"
        } catch {
            Write-Log "[WARN] Could not reach backend for a status update - will retry: $($_.Exception.Message)" "Orange"
            return
        }

        for ($i = $script:ShownLogCount; $i -lt $Current.log_lines.Count; $i++) {
            $line = $Current.log_lines[$i]
            Write-Log $line (Get-LogColor $line)
        }
        $script:ShownLogCount = $Current.log_lines.Count

        if ($Current.status -eq 'succeeded') {
            $script:PollTimer.Stop()
            $ProgressBar.Visible = $false
            Write-Log "[i] Full log saved to: $script:LogFilePath" "Gray"
            $SubmitButton.Enabled = $true; $ResetButton.Enabled = $true
        } elseif ($Current.status -eq 'failed') {
            $script:PollTimer.Stop()
            $ProgressBar.Visible = $false
            if ($Current.error_message) { Write-Log "[FAIL] $($Current.error_message)" "Red" }
            Write-Log "[i] Full log saved to: $script:LogFilePath" "Gray"
            $SubmitButton.Enabled = $true; $ResetButton.Enabled = $true
        }
    })
    $ProgressBar.Visible = $true
    $script:PollTimer.Start()
})
#endregion

[void]$Form.ShowDialog()
