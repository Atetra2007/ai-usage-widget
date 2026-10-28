$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv'
Write-Host ''
Write-Host '=== AI Usage Widget: 准备源码运行环境 / set up dev environment ===' -ForegroundColor Cyan

$cands = @(
  (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
  (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
  (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
)
$py = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) {
  foreach ($v in '3.13','3.12','3.11') {
    try { $p = (& py -$v -c "import sys;print(sys.executable)" 2>$null); if ($p) { $py = $p.Trim(); break } } catch {}
  }
}
if (-not $py) { throw '没找到 Python 3.11-3.13（pywebview 暂不支持 3.14），请截图发给 Claude' }
Write-Host "[1/3] 使用 $py"

if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
  Write-Host '[2/3] 创建独立虚拟环境 .venv（不影响系统 Python）...'
  & $py -m venv $Venv
} else { Write-Host '[2/3] .venv 已存在，跳过' }

Write-Host '[3/3] 安装依赖 / installing requirements ...'
& (Join-Path $Venv 'Scripts\python.exe') -m pip install --upgrade pip --quiet
& (Join-Path $Venv 'Scripts\python.exe') -m pip install -r (Join-Path $Root 'requirements.txt') --quiet
& (Join-Path $Venv 'Scripts\python.exe') -c "import webview; print('pywebview', webview.__version__ if hasattr(webview,'__version__') else 'ok')"

Write-Host ''
Write-Host '完成。双击 start_widget.bat 启动小组件。' -ForegroundColor Green
