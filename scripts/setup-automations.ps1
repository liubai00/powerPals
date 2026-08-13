param(
    [Alias("Profile")][string]$OpenClawProfile = "weather-agent",
    [string]$ManifestPath,
    [string]$DeliveryTarget,
    [string[]]$DeliveryTargets,
    [string]$Location,
    [switch]$Disable,
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:OpenClawProfileName = $OpenClawProfile

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $repositoryRoot "config/automations/weather-schedules.json"
}
$ManifestPath = [IO.Path]::GetFullPath($ManifestPath)

function Assert-RepositoryPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $rootPrefix = $repositoryRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $Path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay inside the repository: $Path"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label does not exist: $Path"
    }
}

function Resolve-ManifestFile {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $manifestDirectory = Split-Path -Parent $ManifestPath
    $resolved = if ([IO.Path]::IsPathRooted($RelativePath)) {
        [IO.Path]::GetFullPath($RelativePath)
    } else {
        [IO.Path]::GetFullPath((Join-Path $manifestDirectory $RelativePath))
    }
    Assert-RepositoryPath -Path $resolved -Label $Label
    return $resolved
}

function Get-ProfileEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)

    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue.Trim()
    }

    $userProfilePath = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if ([string]::IsNullOrWhiteSpace($userProfilePath)) {
        return $null
    }
    $profileDirectoryName = if ($script:OpenClawProfileName -eq "default") { ".openclaw" } else { ".openclaw-$script:OpenClawProfileName" }
    $profileEnvPath = Join-Path (Join-Path $userProfilePath $profileDirectoryName) ".env"
    if (-not (Test-Path -LiteralPath $profileEnvPath -PathType Leaf)) {
        return $null
    }

    $pattern = "^\s*" + [Regex]::Escape($Name) + "\s*=\s*(.*)\s*$"
    foreach ($line in Get-Content -LiteralPath $profileEnvPath -Encoding utf8) {
        if ($line -notmatch $pattern) {
            continue
        }
        $value = $matches[1].Trim()
        if ($value.Length -ge 2) {
            $isDoubleQuoted = $value.StartsWith('"') -and $value.EndsWith('"')
            $isSingleQuoted = $value.StartsWith("'") -and $value.EndsWith("'")
            if ($isDoubleQuoted -or $isSingleQuoted) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        return $value.Trim()
    }
    return $null
}

function Get-NormalizedDeliveryTargets {
    param([string[]]$Values)

    $normalized = [Collections.Generic.List[string]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($value in @($Values)) {
        if ([string]::IsNullOrWhiteSpace($value)) {
            continue
        }
        foreach ($candidate in @($value -split "[,;]")) {
            $target = $candidate.Trim()
            if ([string]::IsNullOrWhiteSpace($target)) {
                continue
            }
            if ($target.Length -gt 200 -or $target -match "[\r\n]") {
                throw "Each delivery target must be a single line with at most 200 characters."
            }
            if (-not $target.StartsWith("chat:", [StringComparison]::OrdinalIgnoreCase)) {
                throw "Weather schedule delivery targets must use the Feishu group form 'chat:<chat_id>': $target"
            }
            if ($seen.Add($target)) {
                $normalized.Add($target)
            }
        }
    }
    return @($normalized)
}

function Get-FanOutInstructions {
    param(
        [string[]]$SecondaryTargets,
        [string]$Channel,
        [string]$Account
    )

    if ($null -eq $SecondaryTargets -or $SecondaryTargets.Length -eq 0) {
        return ""
    }

    $targetLines = @($SecondaryTargets | ForEach-Object { '- `{0}`' -f $_ }) -join "`n"
    return @(
        "## Multi-group delivery",
        "",
        "OpenClaw automatically delivers your final answer to the primary group. Send an identical copy of the completed report to each additional group below:",
        "",
        $targetLines,
        "",
        "- Complete the whole report first. Then call the message tool once per additional target with action=send, channel=$Channel, accountId=$Account, target set to that group, and message set to the complete final report.",
        "- Do not use the message tool for the primary group; the cron delivery owns that copy.",
        "- If the task rules require NO_REPLY, do not call the message tool and return exactly NO_REPLY.",
        "- If one additional send fails, continue with the other targets and still return the complete final report for primary delivery.",
        "- Group ids and delivery operations are internal details. Never include them in the report body."
    ) -join "`n"
}

Assert-RepositoryPath -Path $ManifestPath -Label "Automation manifest"
$manifest = Get-Content -LiteralPath $ManifestPath -Encoding utf8 -Raw | ConvertFrom-Json

if ($manifest.schemaVersion -ne 1) {
    throw "Unsupported automation manifest schemaVersion: $($manifest.schemaVersion)"
}
if ([string]::IsNullOrWhiteSpace([string]$manifest.timezone)) {
    throw "Automation manifest timezone is required."
}
if ([string]::IsNullOrWhiteSpace([string]$manifest.agent)) {
    throw "Automation manifest agent is required."
}
if ([string]::IsNullOrWhiteSpace([string]$manifest.morningBaselineFile)) {
    throw "Automation manifest morningBaselineFile is required."
}
if ([string]::IsNullOrWhiteSpace([string]$manifest.analysisGuide)) {
    throw "Automation manifest analysisGuide is required."
}

$jobs = @($manifest.jobs)
if ($jobs.Count -eq 0) {
    throw "Automation manifest must contain at least one job."
}

$responseTemplatePath = Resolve-ManifestFile -RelativePath ([string]$manifest.responseTemplate) -Label "Response template"
$responseTemplate = Get-Content -LiteralPath $responseTemplatePath -Encoding utf8 -Raw
if (-not $responseTemplate.Contains('```plaintext')) {
    throw "Response template must contain a plaintext data-block example."
}
$analysisGuidePath = Resolve-ManifestFile -RelativePath ([string]$manifest.analysisGuide) -Label "Analysis guide"
$analysisGuide = Get-Content -LiteralPath $analysisGuidePath -Encoding utf8 -Raw
if (-not $analysisGuide.Contains("{{LOCATION}}")) {
    throw "Analysis guide must contain the {{LOCATION}} token."
}
$workspaceRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot "workspace"))
$baselineRelativePath = [string]$manifest.morningBaselineFile
if ([IO.Path]::IsPathRooted($baselineRelativePath)) {
    throw "Automation morningBaselineFile must be relative to the Agent workspace."
}
$baselinePath = [IO.Path]::GetFullPath((Join-Path $workspaceRoot $baselineRelativePath))
$workspacePrefix = $workspaceRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $baselinePath.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Automation morningBaselineFile must stay inside the Agent workspace."
}
$seenKeys = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$seenNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
$promptPaths = @{}

foreach ($job in $jobs) {
    foreach ($requiredProperty in @("declarationKey", "name", "displayName", "description", "cron", "prompt")) {
        if ([string]::IsNullOrWhiteSpace([string]$job.$requiredProperty)) {
            throw "Automation job is missing required property '$requiredProperty'."
        }
    }
    if (-not $seenKeys.Add([string]$job.declarationKey)) {
        throw "Duplicate automation declarationKey: $($job.declarationKey)"
    }
    if (-not $seenNames.Add([string]$job.name)) {
        throw "Duplicate automation name: $($job.name)"
    }
    $cronFields = @(([string]$job.cron -split "\s+") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($cronFields.Count -notin @(5, 6)) {
        throw "Automation '$($job.name)' must use a 5-field or 6-field cron expression."
    }
    $promptPaths[[string]$job.declarationKey] = Resolve-ManifestFile -RelativePath ([string]$job.prompt) -Label "Prompt for '$($job.name)'"
    $promptText = Get-Content -LiteralPath $promptPaths[[string]$job.declarationKey] -Encoding utf8 -Raw
    if (-not $promptText.Contains("{{LOCATION}}")) {
        throw "Prompt for '$($job.name)' must contain the {{LOCATION}} token."
    }
    if ($null -ne $job.PSObject.Properties["additionalTools"]) {
        foreach ($toolName in @($job.additionalTools)) {
            if ([string]::IsNullOrWhiteSpace([string]$toolName)) {
                throw "Automation '$($job.name)' has a blank additional tool name."
            }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Location)) {
    $Location = Get-ProfileEnvironmentValue -Name "WEATHER_SCHEDULE_LOCATION"
}
if ([string]::IsNullOrWhiteSpace($Location)) {
    $Location = [string]$manifest.defaultLocation
}
$Location = $Location.Trim()
if ($Location.Length -gt 100 -or $Location -match "[\r\n]") {
    throw "Schedule location must be a single line with at most 100 characters."
}

$configuredTargets = @()
if ($null -ne $DeliveryTargets -and $DeliveryTargets.Length -gt 0) {
    $configuredTargets = @(Get-NormalizedDeliveryTargets -Values $DeliveryTargets)
} elseif (-not [string]::IsNullOrWhiteSpace($DeliveryTarget)) {
    $configuredTargets = @(Get-NormalizedDeliveryTargets -Values @($DeliveryTarget))
} else {
    $environmentTargets = Get-ProfileEnvironmentValue -Name "WEATHER_SCHEDULE_FEISHU_TARGETS"
    if (-not [string]::IsNullOrWhiteSpace($environmentTargets)) {
        $configuredTargets = @(Get-NormalizedDeliveryTargets -Values @($environmentTargets))
    } else {
        $legacyEnvironmentTarget = Get-ProfileEnvironmentValue -Name "WEATHER_SCHEDULE_FEISHU_TARGET"
        if (-not [string]::IsNullOrWhiteSpace($legacyEnvironmentTarget)) {
            $configuredTargets = @(Get-NormalizedDeliveryTargets -Values @($legacyEnvironmentTarget))
        }
    }
}

$targetConfigured = $configuredTargets.Length -gt 0
$primaryDeliveryTarget = if ($targetConfigured) { [string]$configuredTargets[0] } else { "" }
$secondaryDeliveryTargets = @()
if ($configuredTargets.Length -gt 1) {
    $secondaryDeliveryTargets = @($configuredTargets | Select-Object -Skip 1)
}
$fanOutInstructions = Get-FanOutInstructions -SecondaryTargets $secondaryDeliveryTargets -Channel ([string]$manifest.delivery.channel) -Account ([string]$manifest.delivery.account)
foreach ($job in $jobs) {
    $taskPrompt = Get-Content -LiteralPath $promptPaths[[string]$job.declarationKey] -Encoding utf8 -Raw
    $renderedMessage = ($taskPrompt.Trim() + "`n`n---`n`n" + $analysisGuide.Trim() + "`n`n---`n`n" + $responseTemplate.Trim()).Replace("{{LOCATION}}", $Location).Replace("{{MORNING_BASELINE_FILE}}", $baselineRelativePath.Replace("\", "/"))
    if (-not [string]::IsNullOrWhiteSpace($fanOutInstructions)) {
        $renderedMessage = $renderedMessage.Trim() + "`n`n---`n`n" + $fanOutInstructions.Trim()
    }
    if ($renderedMessage -match "\{\{[^{}]+\}\}") {
        throw "Automation '$($job.name)' contains an unresolved prompt token."
    }
}
if ($ValidateOnly) {
    Write-Host "Validated $($jobs.Count) weather automations for '$Location' in $($manifest.timezone)."
    Write-Host "Delivery targets configured: $($configuredTargets.Length)"
    exit 0
}

$openClawCommand = Get-Command "openclaw.ps1" -ErrorAction SilentlyContinue
if ($null -eq $openClawCommand) {
    $openClawCommand = Get-Command "openclaw.cmd" -ErrorAction SilentlyContinue
}
if ($null -eq $openClawCommand) {
    $openClawCommand = Get-Command "openclaw" -ErrorAction Stop
}
$baseTools = @($manifest.execution.tools)
$enabledCount = 0
New-Item -ItemType Directory -Path (Split-Path -Parent $baselinePath) -Force | Out-Null

foreach ($job in $jobs) {
    $taskPrompt = Get-Content -LiteralPath $promptPaths[[string]$job.declarationKey] -Encoding utf8 -Raw
    $message = ($taskPrompt.Trim() + "`n`n---`n`n" + $analysisGuide.Trim() + "`n`n---`n`n" + $responseTemplate.Trim()).Replace("{{LOCATION}}", $Location).Replace("{{MORNING_BASELINE_FILE}}", $baselineRelativePath.Replace("\", "/"))
    if (-not [string]::IsNullOrWhiteSpace($fanOutInstructions)) {
        $message = $message.Trim() + "`n`n---`n`n" + $fanOutInstructions.Trim()
    }
    $shouldEnable = ($job.enabled -eq $true) -and (-not $Disable) -and $targetConfigured
    $additionalTools = if ($null -ne $job.PSObject.Properties["additionalTools"]) { @($job.additionalTools) } else { @() }
    $fanOutTools = if ($secondaryDeliveryTargets.Length -gt 0) { @("message") } else { @() }
    $jobTools = @($baseTools + $additionalTools + $fanOutTools | Select-Object -Unique)
    $tools = $jobTools -join ","

    $cronArguments = @(
        "--profile", $OpenClawProfile,
        "cron", "add",
        "--json",
        "--declaration-key", [string]$job.declarationKey,
        "--display-name", [string]$job.displayName,
        "--name", [string]$job.name,
        "--description", [string]$job.description,
        "--cron", [string]$job.cron,
        "--tz", [string]$manifest.timezone,
        "--exact",
        "--agent", [string]$manifest.agent,
        "--session", "isolated",
        "--message", $message,
        "--tools", $tools,
        "--thinking", [string]$manifest.execution.thinking,
        "--timeout-seconds", [string]$manifest.execution.timeoutSeconds,
        "--expect-final"
    )

    if ($shouldEnable) {
        $cronArguments += @(
            "--announce",
            "--best-effort-deliver",
            "--channel", [string]$manifest.delivery.channel,
            "--account", [string]$manifest.delivery.account,
            "--to", $primaryDeliveryTarget
        )
    } else {
        $cronArguments += @("--no-deliver", "--disabled")
    }

    $rawResult = & $openClawCommand.Source @cronArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to declare automation '$($job.name)': $($rawResult | Out-String)"
    }
    $result = $rawResult | Out-String | ConvertFrom-Json
    $declaredJob = if ($null -ne $result.job) { $result.job } else { $result }
    $jobId = [string]$declaredJob.id
    if ([string]::IsNullOrWhiteSpace($jobId)) {
        throw "OpenClaw did not return an id for automation '$($job.name)'."
    }

    if ($shouldEnable) {
        $editResult = & $openClawCommand.Source --profile $OpenClawProfile cron edit $jobId --name ([string]$job.name) --description ([string]$job.description) --enable --clear-session-key 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Automation '$($job.name)' was declared but could not be enabled: $($editResult | Out-String)"
        }
    } else {
        $editResult = & $openClawCommand.Source --profile $OpenClawProfile cron edit $jobId --name ([string]$job.name) --description ([string]$job.description) --disable --no-deliver --clear-to --clear-channel --clear-account --clear-session-key 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Automation '$($job.name)' was declared but could not be disabled safely: $($editResult | Out-String)"
        }
    }

    $verifiedJob = (& $openClawCommand.Source --profile $OpenClawProfile cron get $jobId 2>&1) | Out-String | ConvertFrom-Json
    $expectedDeliveryMode = if ($shouldEnable) { "announce" } else { "none" }
    $actualTools = @($verifiedJob.payload.toolsAllow)
    if ([bool]$verifiedJob.enabled -ne $shouldEnable) {
        throw "Automation '$($job.name)' enabled state did not converge."
    }
    if ([string]$verifiedJob.delivery.mode -ne $expectedDeliveryMode) {
        throw "Automation '$($job.name)' delivery mode did not converge."
    }
    if ($shouldEnable -and [string]$verifiedJob.delivery.to -ne $primaryDeliveryTarget) {
        throw "Automation '$($job.name)' primary delivery target did not converge."
    }
    if ([string]$verifiedJob.payload.message -ne $message) {
        throw "Automation '$($job.name)' prompt was truncated or changed during synchronization."
    }
    if ([string]$verifiedJob.name -ne [string]$job.name) {
        throw "Automation '$($job.name)' internal name did not converge."
    }
    if ([string]$verifiedJob.description -ne [string]$job.description) {
        throw "Automation '$($job.name)' description did not converge."
    }
    if (($actualTools -join ",") -ne $tools) {
        throw "Automation '$($job.name)' tool allow-list did not converge."
    }

    if ($shouldEnable) {
        $enabledCount += 1
        Write-Host "Synced and enabled: $($job.name)"
    } else {
        Write-Host "Synced but disabled: $($job.name)"
    }
}

if (-not $targetConfigured -and -not $Disable) {
    Write-Warning "No WEATHER_SCHEDULE_FEISHU_TARGETS is configured. All weather automations remain disabled to prevent accidental delivery."
}
Write-Host "Weather automations synchronized: $($jobs.Count) total, $enabledCount enabled."
