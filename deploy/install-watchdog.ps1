<#
.SYNOPSIS
  Guardian 宿主看门狗 —— Windows 一键安装 / 更新。

.DESCRIPTION
  为什么需要这个脚本：插件（容器内）把睡眠请求写到
  /AstrBot/data/guardian/restart_request.json，映射到宿主是
  <compose>\data\astrbot\guardian\restart_request.json；而看门狗白名单里的
  容器名又必须和实际 `docker compose ps` 出来的名字一致。这三处任何一处
  手工写错，请求都会写出去无人消费，而且失败是静默的。本脚本全部自动探测。

.EXAMPLE
  # 在当前目录向上找 compose.yml，并注册每 30 秒一次的计划任务
  powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1

.EXAMPLE
  # 只打印将要写入的配置，不落地
  powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1 -DryRun

.EXAMPLE
  # 容器名自动探测失败时手工指定
  powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1 -Targets astrbot-local,napcat-local

.EXAMPLE
  # 卸载计划任务
  powershell -ExecutionPolicy Bypass -File .\install-watchdog.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$ComposeDir,
    [string[]]$Targets,
    [string]$DataRoot,
    [string]$TaskName = "GuardianWatchdog",
    [int]$IntervalSeconds = 30,
    [switch]$DryRun,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Write-Note([string]$Text) {
    Write-Host "  $Text"
}

function Die([string]$Text) {
    Write-Host "错误：$Text" -ForegroundColor Red
    exit 1
}

# 计划任务需要管理员；写 ProgramData 同理。
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principalCheck = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principalCheck.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Die "需要以管理员身份运行 PowerShell" }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$watchdogPy = Join-Path $here "guardian-watchdog.py"
if (-not (Test-Path $watchdogPy)) { Die "脚本同目录缺少 guardian-watchdog.py" }

$dataHome = Join-Path $env:ProgramData "guardian"
$envFile = Join-Path $dataHome "guardian-watchdog.env"
$logFile = Join-Path $dataHome "guardian-watchdog.log"

# --------------------------------------------------------------------- #
if ($Uninstall) {
    Write-Step "卸载"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Note "已删除计划任务 $TaskName"
    } else {
        Write-Note "计划任务 $TaskName 不存在，跳过"
    }
    Write-Note "$envFile 与 $logFile 保留（内含你的容器白名单）"
    exit 0
}

# --------------------------------------------------------------------- #
Write-Step "1/6 定位 compose 目录"
if (-not $ComposeDir) {
    $candidates = @("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")
    $probe = Get-Location
    while ($probe) {
        foreach ($name in $candidates) {
            $candidate = Join-Path $probe $name
            if (Test-Path $candidate) { $ComposeDir = $probe; break }
        }
        if ($ComposeDir) { break }
        $probe = $probe.Parent
    }
}
if (-not $ComposeDir) { Die "找不到 compose.yml，请用 -ComposeDir 指定" }
$ComposeDir = (Resolve-Path $ComposeDir).Path
Write-Note "compose 目录：$ComposeDir"

# --------------------------------------------------------------------- #
Write-Step "2/6 探测数据目录"
if (-not $DataRoot) {
    # 优先读 compose 里 `./data:/AstrBot/data` 这类挂载
    $composeFile = Get-ChildItem -Path $ComposeDir -File |
        Where-Object { $_.Name -match '^docker-?compose\.ya?ml$' -or $_.Name -match '^compose\.ya?ml$' } |
        Select-Object -First 1
    if ($composeFile) {
        $hit = Select-String -Path $composeFile.FullName -Pattern '-+\s*([^:\s]+)\s*:\s*/AstrBot/data' |
            Select-Object -First 1
        if ($hit) {
            $hostPath = $hit.Matches[0].Groups[1].Value
            if ($hostPath -match '^[A-Za-z]:|^\\\\') {
                $DataRoot = $hostPath
            } else {
                $DataRoot = Join-Path $ComposeDir ($hostPath -replace '^\./|^\.\\', '')
            }
        }
    }
    if (-not $DataRoot) { $DataRoot = Join-Path $ComposeDir "data" }
}
if (-not (Test-Path $DataRoot)) { Die "数据目录不存在：$DataRoot（先用 docker compose up -d 起一次）" }
$DataRoot = (Resolve-Path $DataRoot).Path
Write-Note "数据目录：$DataRoot"
if (-not (Test-Path (Join-Path $DataRoot "guardian"))) {
    Write-Note "提示：$DataRoot\guardian 尚未创建，看门狗启动时会自动建"
}

# --------------------------------------------------------------------- #
Write-Step "3/6 探测容器名"
# 只认 `docker compose ps` 的**真实容器名**：看门狗的白名单按容器名比对，
# 猜错一个字母就是 target-not-allowed，而且是静默失败。
$names = @()
$savedLocation = Get-Location
Set-Location $ComposeDir
$raw = @(& docker compose ps --format "{{.Name}}" 2>$null)
$rawExit = $LASTEXITCODE
if ($rawExit -eq 0) {
    foreach ($item in $raw) {
        $trimmed = "$item".Trim()
        if ($trimmed) { $names += $trimmed }
    }
}
if (@($names).Length -eq 0) {
    $rawJson = @(& docker compose ps --format json 2>$null)
    $jsonExit = $LASTEXITCODE
    if ($jsonExit -eq 0) {
        foreach ($item in $rawJson) {
            if (-not $item) { continue }
            $parsedName = $null
            try { $parsedName = ("$item" | ConvertFrom-Json).Name } catch { $parsedName = $null }
            if ($parsedName) { $names += "$parsedName" }
        }
    }
}
Set-Location $savedLocation
$names = @($names | Sort-Object -Unique)

if (@($Targets).Length -eq 0) {
    $Targets = @($names | Where-Object { $_ -notmatch 'kernel' })
}
$Targets = @($Targets | Where-Object { $_ })
if (@($Targets).Length -eq 0) {
    Die "读不到运行中的容器，请用 -Targets 手工指定（如 -Targets astrbot-local,napcat-local）"
}
if (@($Targets).Length -gt 10) { Die "容器数量 $(@($Targets).Length) 超过看门狗上限 10" }
Write-Note "允许的目标：$($Targets -join ' ')"

$kernelContainer = @($names | Where-Object { $_ -match 'kernel' } | Select-Object -First 1)
if (@($kernelContainer).Length -eq 0) { $kernelContainer = "guardian-kernel" }
$kernelContainer = "$(@($kernelContainer)[0])"
Write-Note "内核容器：$kernelContainer"

# --------------------------------------------------------------------- #
Write-Step "4/6 生成配置"
$lines = @(
    "# 由 deploy/install-watchdog.ps1 生成 $(Get-Date -Format o)",
    "# 重新执行安装脚本即可刷新；手改会被下次安装覆盖。",
    "GUARDIAN_COMPOSE_DIR=$ComposeDir",
    "GUARDIAN_DATA_ROOT=$DataRoot",
    "GUARDIAN_ALLOWED_TARGETS=$($Targets -join ' ')",
    "GUARDIAN_DOCKER_BACKEND=compose",
    "GUARDIAN_MAX_AGE_SEC=120",
    "GUARDIAN_CMD_TIMEOUT=120",
    "GUARDIAN_LOG_FILE=$logFile",
    "GUARDIAN_KERNEL_GUARD=1",
    "GUARDIAN_KERNEL_CONTAINER=$kernelContainer",
    "GUARDIAN_KERNEL_HEARTBEAT_TIMEOUT=90",
    "GUARDIAN_KERNEL_RESTART_COOLDOWN=300"
)
if ($DryRun) {
    Write-Note "[dry-run] 将写入 $envFile ："
    $lines | ForEach-Object { Write-Note "  $_" }
    exit 0
}
New-Item -ItemType Directory -Force -Path $dataHome | Out-Null
if (Test-Path $envFile) {
    Copy-Item $envFile "$envFile.bak.$([int](Get-Date -UFormat %s))" -Force
}
Set-Content -Path $envFile -Value $lines -Encoding UTF8
Write-Note "已写入 $envFile"

# --------------------------------------------------------------------- #
Write-Step "5/6 注册计划任务（每 $IntervalSeconds 秒）"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $python) { Die "找不到 python，请先安装 Python 3 并加入 PATH" }
Write-Note "python：$python"

# 看门狗吃环境变量；计划任务不继承 shell，所以用一个薄包装把 env 注入后再执行。
$runner = Join-Path $dataHome "run-watchdog.ps1"
$runnerBody = @'
# 由 install-watchdog.ps1 生成：把 guardian-watchdog.env 注入环境后跑一次 tick。
$ErrorActionPreference = "Stop"
$envFile = "__ENVFILE__"
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $index = $line.IndexOf("=")
            [Environment]::SetEnvironmentVariable(
                $line.Substring(0, $index).Trim(),
                $line.Substring($index + 1).Trim(),
                "Process")
        }
    }
}
& "__PYTHON__" "__WATCHDOG__" --once
exit $LASTEXITCODE
'@
$runnerBody = $runnerBody.Replace("__ENVFILE__", $envFile)
$runnerBody = $runnerBody.Replace("__PYTHON__", $python)
$runnerBody = $runnerBody.Replace("__WATCHDOG__", $watchdogPy)
Set-Content -Path $runner -Value $runnerBody -Encoding UTF8
Write-Note "已写入 $runner"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Seconds $IntervalSeconds)
$principalName = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $principalName -LogonType S4U `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 40)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Note "已注册计划任务 $TaskName"

# --------------------------------------------------------------------- #
Write-Step "6/6 自检"
& $runner
if ($LASTEXITCODE -eq 0) {
    Write-Note "单次执行成功"
} else {
    Write-Note "单次执行返回 $LASTEXITCODE，请查看 $logFile"
}

Write-Host ""
Write-Host "完成。下一步在 WebUI 里确认：" -ForegroundColor Green
Write-Host "  safety.self_restart_strategy = watchdog"
Write-Host "  safety.allow_self_restart     = true    # 否则只有 NapCat 会睡，本体不参与"
Write-Host ""
Write-Host "验证：Get-ScheduledTaskInfo -TaskName $TaskName"
