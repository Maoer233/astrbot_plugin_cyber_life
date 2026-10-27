param(
    [string]$PluginsDir = ""
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginName = "astrbot_plugin_cyber_life"

if (-not $PluginsDir) {
    $candidates = Get-ChildItem "$env:USERPROFILE\.astrbot_launcher\instances" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "core\data\plugins" } |
        Where-Object { Test-Path $_ }
    if ($candidates.Count -eq 0) {
        throw "没有找到 AstrBot 插件目录。请用 -PluginsDir 指定 core\data\plugins 的路径。"
    }
    if ($candidates.Count -gt 1) {
        Write-Host "发现多个 AstrBot 实例：" -ForegroundColor Yellow
        $candidates | ForEach-Object { Write-Host "  $_" }
        throw "请用 -PluginsDir 指定要安装到哪个实例。"
    }
    $PluginsDir = $candidates[0]
}

if (-not (Test-Path $PluginsDir)) {
    throw "插件目录不存在：$PluginsDir"
}

$target = Join-Path $PluginsDir $PluginName
Write-Host "源目录：$Here"
Write-Host "目标目录：$target"

if (Test-Path $target) {
    # Keep backups outside data/plugins so AstrBot never scans them as a
    # second plugin with the same metadata.
    $backupRoot = Join-Path (Split-Path $PluginsDir -Parent) "plugin_backups"
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backup = Join-Path $backupRoot "$PluginName-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Write-Host "目标已存在，先备份到 $backup" -ForegroundColor Yellow
    Move-Item $target $backup
}

New-Item -ItemType Directory -Path $target | Out-Null
robocopy $Here $target /E /XD "__pycache__" ".test-tmp" ".git" /XF "*.pyc" | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "复制失败（robocopy exit $LASTEXITCODE）"
}

$size = [math]::Round(((Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "完成：$target（$size MB）" -ForegroundColor Green
Write-Host ""
Write-Host "下一步："
Write-Host "  1. 打开 AstrBot WebUI → 插件 → 重载插件"
Write-Host "  2. 进入“赛博日常”插件详情 → 打开“赛博日常控制台”"
Write-Host "  3. 账号页扫码登录 B站 / 小黑盒 / 贴吧"
Write-Host ""
Write-Host "浏览器/二维码/ffmpeg 依赖已随插件 vendored。"
Write-Host "本地语音听写需要 faster-whisper；未安装时B站AI字幕仍可用。"
Write-Host "浏览器默认使用本机 Edge；若失败，在设置页切换浏览器通道或填写可执行文件路径。"
