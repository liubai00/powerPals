param(
    [string]$Profile = "weather-agent",
    [string]$ComfyBaseUrl = "http://127.0.0.1:8188",
    [string]$ComfyWorkflowPath
)

$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspacePath = (Resolve-Path (Join-Path $repositoryRoot "workspace")).Path
$pluginPath = (Resolve-Path (Join-Path $repositoryRoot "plugins\weather-query-tools")).Path
if ([string]::IsNullOrWhiteSpace($ComfyWorkflowPath)) {
    $ComfyWorkflowPath = (Resolve-Path (Join-Path $repositoryRoot "config\workflows\z-image-turbo-nvfp4-api.json")).Path
}

$openClawCommand = Get-Command "openclaw.cmd" -ErrorAction Stop
$npmCommand = Get-Command "npm.cmd" -ErrorAction Stop
$openClawVersion = & $openClawCommand.Source --version
$officialPlugins = @(
    @{
        Id = "feishu"
        Version = "2026.7.1"
        Spec = "@openclaw/feishu@2026.7.1"
    },
    @{
        Id = "tavily"
        Version = "2026.7.1"
        Spec = "@openclaw/tavily-plugin@2026.7.1"
    },
    @{
        Id = "llama-cpp"
        Version = "2026.7.1"
        Spec = "clawhub:@openclaw/llama-cpp-provider"
    }
)

if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the OpenClaw version."
}

if ($openClawVersion -notmatch "2026\.7\.1-2") {
    Write-Warning "This repository is pinned to OpenClaw 2026.7.1-2; installed: $openClawVersion"
}

& $npmCommand.Source ci --prefix $pluginPath
if ($LASTEXITCODE -ne 0) {
    throw "npm ci failed."
}

& $npmCommand.Source run plugin:validate --prefix $pluginPath
if ($LASTEXITCODE -ne 0) {
    throw "Weather plugin validation failed."
}

$pluginListJson = & $openClawCommand.Source --profile $Profile plugins list --json
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect installed OpenClaw plugins."
}
$installedPlugins = ($pluginListJson | Out-String | ConvertFrom-Json).plugins

foreach ($plugin in $officialPlugins) {
    $installed = $installedPlugins | Where-Object {
        $_.id -eq $plugin.Id -and $_.version -eq $plugin.Version
    } | Select-Object -First 1
    if ($installed) {
        Write-Host "Official plugin '$($plugin.Id)' $($plugin.Version) is already installed."
        continue
    }

    $pluginInstallArgs = @("--profile", $Profile, "plugins", "install", "--force")
    if (-not $plugin.Spec.StartsWith("clawhub:")) {
        $pluginInstallArgs += "--pin"
    }
    $pluginInstallArgs += $plugin.Spec
    & $openClawCommand.Source @pluginInstallArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install official OpenClaw plugin '$($plugin.Spec)'."
    }
}

& $openClawCommand.Source --profile $Profile plugins install --link $pluginPath
if ($LASTEXITCODE -ne 0) {
    throw "Unable to link the weather plugin into profile '$Profile'."
}

$configPatch = @{
    agents = @{
        defaults = @{
            workspace = $workspacePath
            skipBootstrap = $true
            memorySearch = @{
                enabled = $true
                provider = "local"
                fallback = "none"
                sync = @{
                    onSessionStart = $true
                    onSearch = $true
                    watch = $true
                }
            }
            imageGenerationModel = @{
                primary = "comfy/workflow"
                timeoutMs = 600000
            }
        }
    }
    tools = @{
        profile = "minimal"
        alsoAllow = @(
            "group:fs",
            "group:memory",
            "web_search",
            "web_fetch",
            "cron",
            "message",
            "image_generate",
            "qweather_query",
            "openmeteo_query"
        )
        web = @{
            search = @{
                enabled = $true
                provider = "tavily"
            }
        }
    }
    cron = @{
        enabled = $true
    }
    channels = @{
        feishu = @{
            renderMode = "auto"
            markdown = @{
                mode = "native"
                tableMode = "ascii"
            }
        }
    }
    plugins = @{
        allow = @(
            "weather-query-tools",
            "feishu",
            "tavily",
            "llama-cpp",
            "memory-core",
            "active-memory",
            "comfy"
        )
        entries = @{
            "weather-query-tools" = @{
                enabled = $true
                config = @{}
            }
            "feishu" = @{
                enabled = $true
            }
            "tavily" = @{
                enabled = $true
                config = @{
                    webSearch = @{
                        apiKey = @{
                            source = "env"
                            provider = "default"
                            id = "TAVILY_API_KEY"
                        }
                    }
                }
            }
            "llama-cpp" = @{
                enabled = $true
            }
            "memory-core" = @{
                enabled = $true
                config = @{
                    dreaming = @{
                        enabled = $true
                        frequency = "0 3 * * *"
                        timezone = "Asia/Shanghai"
                    }
                }
            }
            "active-memory" = @{
                enabled = $true
                config = @{
                    enabled = $true
                    agents = @("main")
                    allowedChatTypes = @("direct")
                    queryMode = "recent"
                    promptStyle = "preference-only"
                    timeoutMs = 15000
                    maxSummaryChars = 220
                    persistTranscripts = $false
                    logging = $true
                }
            }
            "comfy" = @{
                enabled = $true
                config = @{
                    mode = "local"
                    baseUrl = $ComfyBaseUrl
                    image = @{
                        workflowPath = $ComfyWorkflowPath
                        promptNodeId = "27"
                        promptInputName = "text"
                        outputNodeId = "9"
                        pollIntervalMs = 1500
                        timeoutMs = 600000
                    }
                }
            }
        }
    }
}

$configJson = $configPatch | ConvertTo-Json -Depth 12 -Compress
$configJson | & $openClawCommand.Source --profile $Profile config patch --stdin
if ($LASTEXITCODE -ne 0) {
    throw "Unable to apply the OpenClaw profile configuration."
}

& $openClawCommand.Source --profile $Profile config validate
if ($LASTEXITCODE -ne 0) {
    throw "OpenClaw configuration validation failed."
}

Write-Host "Weather Agent profile '$Profile' is configured."
Write-Host "Next: configure the profile .env, model, and reviewed Feishu targets, then start the gateway."
