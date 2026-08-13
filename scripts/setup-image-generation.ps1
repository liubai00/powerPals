param(
    [string]$InstallRoot,
    [string]$TaskName = "OpenClaw ComfyUI (weather-agent)",
    [switch]$SkipTaskRegistration
)

$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
    $workspaceRoot = Split-Path (Split-Path $repositoryRoot -Parent) -Parent
    $InstallRoot = Join-Path $workspaceRoot "runtime\weather-agent-comfyui"
}

$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$portableRoot = Join-Path $InstallRoot "ComfyUI_windows_portable"
$archivePath = Join-Path $InstallRoot "ComfyUI_windows_portable_nvidia-v0.32.0.7z"
$runScriptPath = (Resolve-Path (Join-Path $PSScriptRoot "run-comfyui.ps1")).Path

$runtime = @{
    Url = "https://github.com/Comfy-Org/ComfyUI/releases/download/v0.32.0/ComfyUI_windows_portable_nvidia.7z"
    Path = $archivePath
    Size = 2132254184
    Sha256 = "642ba5e91c5f6310b11797acf79484d2248df5e09c6bb27696a25c99e68bdb72"
}

$models = @(
    @{
        Url = "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_nvfp4.safetensors"
        RelativePath = "ComfyUI\models\diffusion_models\z_image_turbo_nvfp4.safetensors"
        Size = 4509509600
        Sha256 = "a553c889dbcb910de4c98293237573219a37007c1074a3f04576646a088bd5c8"
    },
    @{
        Url = "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b_fp4_mixed.safetensors"
        RelativePath = "ComfyUI\models\text_encoders\qwen_3_4b_fp4_mixed.safetensors"
        Size = 3479416193
        Sha256 = "7ca32dcf07dfe7692945d80fff86e3a74cb83c6206b9b223ac6836b939bb85d6"
    },
    @{
        Url = "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors"
        RelativePath = "ComfyUI\models\vae\ae.safetensors"
        Size = 335304388
        Sha256 = "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38"
    }
)

function Get-IsVerifiedFile {
    param([hashtable]$Asset)

    if (-not (Test-Path -LiteralPath $Asset.Path -PathType Leaf)) {
        return $false
    }

    $item = Get-Item -LiteralPath $Asset.Path
    if ($item.Length -ne $Asset.Size) {
        return $false
    }

    $actualHash = (Get-FileHash -LiteralPath $Asset.Path -Algorithm SHA256).Hash.ToLowerInvariant()
    return $actualHash -eq $Asset.Sha256
}

function Install-VerifiedFile {
    param([hashtable]$Asset)

    if (Get-IsVerifiedFile -Asset $Asset) {
        Write-Host "Verified: $($Asset.Path)"
        return
    }

    $parentDirectory = Split-Path $Asset.Path -Parent
    New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null

    $curlCommand = Get-Command "curl.exe" -ErrorAction Stop
    & $curlCommand.Source --location --fail --retry 5 --retry-all-errors --continue-at - --output $Asset.Path $Asset.Url
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed: $($Asset.Url)"
    }

    if (-not (Get-IsVerifiedFile -Asset $Asset)) {
        throw "Size or SHA-256 verification failed: $($Asset.Path)"
    }
}

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
Install-VerifiedFile -Asset $runtime

$mainPath = Join-Path $portableRoot "ComfyUI\main.py"
if (-not (Test-Path -LiteralPath $mainPath -PathType Leaf)) {
    $tarCommand = Get-Command "tar.exe" -ErrorAction Stop
    & $tarCommand.Source -xf $archivePath -C $InstallRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to extract the ComfyUI portable archive."
    }
}

foreach ($model in $models) {
    $model.Path = Join-Path $portableRoot $model.RelativePath
    Install-VerifiedFile -Asset $model
}

if (-not $SkipTaskRegistration) {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $taskArguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runScriptPath`" -ComfyRoot `"$portableRoot`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArguments -WorkingDirectory $repositoryRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Loopback-only ComfyUI runtime for the OpenClaw weather agent." `
        -Force | Out-Null

    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Scheduled task '$TaskName' is installed and started."
}

Write-Host "ComfyUI image generation is installed at $portableRoot"
