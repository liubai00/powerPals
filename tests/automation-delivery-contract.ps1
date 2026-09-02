Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$setupScriptPath = Join-Path $repositoryRoot "scripts/setup-automations.ps1"
$manifestPath = Join-Path $repositoryRoot "config/automations/weather-schedules.json"
$source = Get-Content -LiteralPath $setupScriptPath -Encoding utf8 -Raw
$manifest = Get-Content -LiteralPath $manifestPath -Encoding utf8 -Raw | ConvertFrom-Json

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

$jobsByKey = @{}
foreach ($job in @($manifest.jobs)) {
    $jobsByKey[[string]$job.declarationKey] = $job
}

$expectedEnabledState = @{
    "weather-agent.daily.morning-0900" = $true
    "weather-agent.daily.recheck-1630" = $false
    "weather-agent.daily.tomorrow-1700" = $false
}

foreach ($entry in $expectedEnabledState.GetEnumerator()) {
    if (-not $jobsByKey.ContainsKey($entry.Key)) {
        throw "Automation manifest is missing required job: $($entry.Key)"
    }
    if ([bool]$jobsByKey[$entry.Key].enabled -ne [bool]$entry.Value) {
        throw "Automation '$($entry.Key)' enabled state does not match the morning-only delivery policy."
    }
}

Write-Host "Automation delivery contract passed: only the morning report is enabled, all groups use explicit message sends, and cron delivery is disabled."
