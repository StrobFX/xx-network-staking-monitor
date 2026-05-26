param(
    [switch]$TestEmail,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "xx_staking_report.py"
$configPath = Join-Path (Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "xx_staking_report") "email_config.json"
$pythonPrefixArguments = @()

if ($PythonPath) {
    $pythonCommand = $PythonPath
}
elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
    $pythonCommand = "py.exe"
    $pythonPrefixArguments = @("-3")
}
elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
    $pythonCommand = "python.exe"
}
else {
    throw "Python 3 was not found. Install Python or provide -PythonPath."
}

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Script not found: $scriptPath"
}
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Email configuration not found. Run configure_xx_staking_email.ps1 first."
}

$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$securePassword = ConvertTo-SecureString $config.encrypted_password
$credentials = [System.Net.NetworkCredential]::new("", $securePassword)

$env:XX_STAKING_SMTP_PASSWORD = $credentials.Password
try {
    $arguments = @(
        $scriptPath,
        "--days", "30",
        "--email-on-alert",
        "--email-to", $config.recipient,
        "--smtp-user", $config.sender,
        "--smtp-host", $config.smtp_host,
        "--smtp-port", "$($config.smtp_port)"
    )
    if ($TestEmail) {
        $arguments += "--email-test"
    }
    & $pythonCommand @pythonPrefixArguments @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The report exited with code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:\XX_STAKING_SMTP_PASSWORD -ErrorAction SilentlyContinue
}
