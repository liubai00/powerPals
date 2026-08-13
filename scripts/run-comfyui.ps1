param(
    [string]$ComfyRoot,
    [int]$Port = 8188
)

$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($ComfyRoot)) {
    $workspaceRoot = Split-Path (Split-Path $repositoryRoot -Parent) -Parent
    $ComfyRoot = Join-Path $workspaceRoot "runtime\weather-agent-comfyui\ComfyUI_windows_portable"
}

$ComfyRoot = [System.IO.Path]::GetFullPath($ComfyRoot)
$pythonPath = Join-Path $ComfyRoot "python_embeded\python.exe"
$mainPath = Join-Path $ComfyRoot "ComfyUI\main.py"
$logDirectory = Join-Path $ComfyRoot "logs"
$logPath = Join-Path $logDirectory "comfyui.log"
$errorLogPath = Join-Path $logDirectory "comfyui-error.log"

foreach ($requiredPath in @($pythonPath, $mainPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required ComfyUI file not found: $requiredPath"
    }
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

$comfyArguments = @(
    "-s",
    $mainPath,
    "--windows-standalone-build",
    "--listen", "127.0.0.1",
    "--port", $Port.ToString(),
    "--disable-auto-launch",
    "--preview-method", "none",
    "--cache-none",
    "--lowvram",
    "--reserve-vram", "1",
    "--disable-dynamic-vram",
    "--disable-async-offload",
    "--disable-pinned-memory",
    "--disable-all-custom-nodes",
    "--disable-api-nodes",
    "--disable-metadata",
    "--log-stdout"
)

$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList $comfyArguments `
    -WorkingDirectory (Join-Path $ComfyRoot "ComfyUI") `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logPath `
    -RedirectStandardError $errorLogPath `
    -Wait `
    -PassThru

if ($process.ExitCode -ne 0) {
    throw "ComfyUI exited with code $($process.ExitCode). See $logPath and $errorLogPath"
}
