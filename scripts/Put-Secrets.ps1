<#
.SYNOPSIS
    Mirror the secrets in .env into SSM Parameter Store as SecureStrings.

.DESCRIPTION
    The deployed Lambdas carry no credentials in their environment -- only
    SSM_PARAM_PATH -- and read these parameters at cold start (app/config.py).

    This is deliberately a one-time human action rather than part of
    `sam deploy`: SESSION_SECRET and VAPID_PRIVATE_KEY must be set once and
    then persist. Rotating VAPID silently invalidates every push subscription
    on every phone, and rotating SESSION_SECRET logs everyone out, so a
    redeploy must never be able to regenerate them.

    Values are passed to the AWS CLI through a temporary JSON file rather than
    on the command line: PowerShell 5.1's native-argument quoting mangles
    values containing quotes or ampersands, and a password is allowed to
    contain both. The file is removed in a finally block.

.PARAMETER Path
    SSM path prefix. Must match SecretsPath in template.yaml.

.EXAMPLE
    just put-secrets
    powershell -File scripts/Put-Secrets.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]   $Path    = '/hydro-script/prod',
    [string]   $Region  = 'us-east-1',
    [string]   $EnvFile = '.env',
    [string[]] $Only,
    [switch]   $DryRun
)

$ErrorActionPreference = 'Stop'

# What the app actually reads. VAPID_SUBJECT is optional (push.py falls back to
# IAQUALINK_USER); everything else must be present or the function is broken.
$Required = @('IAQUALINK_USER', 'IAQUALINK_PASS', 'SESSION_SECRET', 'VAPID_PRIVATE_KEY')
$Optional = @('VAPID_SUBJECT')

if (-not (Test-Path $EnvFile)) { throw "No env file at '$EnvFile'." }

# Parse KEY=VALUE, skipping blanks and comments. Strips one layer of matched
# quotes, the way dotenv does, so a quoted .env value uploads unquoted.
$values = @{}
foreach ($line in (Get-Content $EnvFile)) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }
    $key = $trimmed.Substring(0, $split).Trim()
    $val = $trimmed.Substring($split + 1).Trim()
    if ($val.Length -ge 2 -and (
            ($val.StartsWith('"') -and $val.EndsWith('"')) -or
            ($val.StartsWith("'") -and $val.EndsWith("'")))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    $values[$key] = $val
}

$names = $Required + $Optional
if ($Only) { $names = $names | Where-Object { $Only -contains $_ } }

$missing = $Required | Where-Object { -not $values.ContainsKey($_) -or $values[$_] -eq '' }
if ($missing -and -not $Only) {
    throw "Missing from ${EnvFile}: $($missing -join ', ')"
}

$prefix = $Path.TrimEnd('/')
foreach ($name in $names) {
    if (-not $values.ContainsKey($name) -or $values[$name] -eq '') {
        Write-Host "skip  $name (not set in $EnvFile)" -ForegroundColor DarkGray
        continue
    }

    $full = "$prefix/$name"
    if ($DryRun) {
        Write-Host "would put  $full  ($($values[$name].Length) chars)" -ForegroundColor Yellow
        continue
    }

    # --overwrite so re-running is safe; the values are the source of truth in
    # .env until the day they are rotated deliberately.
    $payload = @{
        Name      = $full
        Value     = $values[$name]
        Type      = 'SecureString'
        Overwrite = $true
        Tier      = 'Standard'
    } | ConvertTo-Json -Compress

    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        # UTF-8 with no BOM: the AWS CLI's file:// reader chokes on a BOM,
        # which is what Out-File / Set-Content would write here by default.
        [System.IO.File]::WriteAllText($tmp, $payload, [System.Text.UTF8Encoding]::new($false))
        $result = aws ssm put-parameter --cli-input-json "file://$tmp" --region $Region
        if ($LASTEXITCODE -ne 0) { throw "put-parameter failed for $full" }
        $version = ($result | ConvertFrom-Json).Version
        Write-Host "put   $full  (version $version)" -ForegroundColor Green
    }
    finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

if (-not $DryRun) {
    Write-Host ""
    Write-Host "Verify (names and metadata only, no values):" -ForegroundColor Cyan
    Write-Host "  aws ssm get-parameters-by-path --path $prefix --region $Region --query 'Parameters[].Name'"
}
