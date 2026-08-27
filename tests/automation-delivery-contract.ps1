Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$setupScriptPath = Join-Path $repositoryRoot "scripts/setup-automations.ps1"
$source = Get-Content -LiteralPath $setupScriptPath -Encoding utf8 -Raw

$requiredFragments = @(
    "function Get-ExplicitDeliveryInstructions",
    "Send an identical copy of the completed report to every group below",
    '$deliveryTools = if ($targetConfigured) { @("message") } else { @() }',
    '$cronArguments += @("--no-deliver")',
    "--enable --no-deliver --clear-to --clear-channel --clear-account",
    '$expectedDeliveryMode = "none"',
    "After all attempts, return exactly NO_REPLY"
)

foreach ($fragment in $requiredFragments) {
    if (-not $source.Contains($fragment)) {
        throw "Automation delivery contract is missing required fragment: $fragment"
    }
}

$forbiddenFragments = @(
    "OpenClaw automatically delivers your final answer to the primary group",
    "--announce",
    '$primaryDeliveryTarget',
    "Get-FanOutInstructions"
)

foreach ($fragment in $forbiddenFragments) {
    if ($source.Contains($fragment)) {
        throw "Automation delivery contract contains unsafe fragment: $fragment"
    }
}

Write-Host "Automation delivery contract passed: all groups use explicit message sends and cron delivery is disabled."
