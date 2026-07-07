#requires -Version 7.0
<#
.SYNOPSIS
    Convenience wrapper: load secrets from .env (if present) and run one of
    this repo's scripts (default: Get-OfflineEndpoints.ps1). The PowerShell
    twin of run.sh.

.DESCRIPTION
    - Runs from the script's own folder, so it works no matter where you
      invoke it from.
    - If a .env file exists next to it, loads each NAME=value line into the
      environment (the PowerShell equivalent of `set -a; source .env`).
      If there is no .env, it just relies on whatever TMV1_TOKEN /
      TMV1_REGION_URL are already set (env var, SecretManagement, etc.).
    - If the first argument ends in .ps1, it's used as the script to run
      instead of the default; everything else is forwarded, e.g.
        ./run.ps1 Remove-OfflineEndpoints.ps1 -Verify
    - Otherwise all parameters you pass are forwarded to the default script, e.g.
        ./run.ps1 -OfflineHours 24 -HostnamePrefix iws

.EXAMPLE
    ./run.ps1

.EXAMPLE
    ./run.ps1 -OfflineHours 24

.EXAMPLE
    ./run.ps1 Remove-OfflineEndpoints.ps1 -Verify
#>

# NOTE: intentionally NOT [CmdletBinding()] and no param() block. An advanced
# script leaves the automatic $args unpopulated ($null), and `& $script @args`
# would then splat $null as a positional argument — binding it to the child's
# first parameter ($Token) and wiping out its $env:TMV1_TOKEN default. A plain
# script populates $args as a real array, so pass-through args forward cleanly.

$ErrorActionPreference = "Stop"

# Always operate from this script's own directory.
Set-Location -Path $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { return }   # skip blanks/comments
        if ($line -notmatch "=") { return }                       # skip malformed lines

        $name, $value = $line -split "=", 2
        $name  = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")                # strip optional quotes
        Set-Item -Path "Env:$name" -Value $value
    }
    Write-Host "Loaded environment from .env"
} else {
    Write-Host "No .env found; using existing environment variables."
}

if ([string]::IsNullOrWhiteSpace($env:TMV1_TOKEN)) {
    Write-Error "TMV1_TOKEN is not set. Create a .env (copy .env.example) or set `$env:TMV1_TOKEN before running."
    exit 1
}

if ($args.Count -gt 0 -and $args[0] -like "*.ps1") {
    $scriptName = $args[0]
    $forwardArgs = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }
} else {
    $scriptName = "Get-OfflineEndpoints.ps1"
    $forwardArgs = $args
}

$script = Join-Path $PSScriptRoot $scriptName
& $script @forwardArgs
