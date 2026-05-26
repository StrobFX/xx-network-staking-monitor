param(
    [Parameter(Mandatory = $true)]
    [string]$Sender,
    [string]$Recipient = ""
)

$ErrorActionPreference = "Stop"
if (-not $Recipient) {
    $Recipient = $Sender
}

$configDirectory = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "xx_staking_report"
$configPath = Join-Path $configDirectory "email_config.json"

Write-Host "Configuring Gmail notifications for the xx Network staking report."
Write-Host "Use a Google app password, never your regular Google password."
$password = Read-Host "Google app password for $Sender" -AsSecureString
if ($password.Length -eq 0) {
    throw "The app password cannot be empty."
}

New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
$config = [ordered]@{
    sender = $Sender
    recipient = $Recipient
    smtp_host = "smtp.gmail.com"
    smtp_port = 465
    encrypted_password = ConvertFrom-SecureString -SecureString $password
}
$config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8

Write-Host "Configuration saved for the current Windows user:"
Write-Host $configPath
Write-Host "Test delivery with: .\run_xx_staking_monitor.ps1 -TestEmail"
