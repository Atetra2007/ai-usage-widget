$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { throw '先运行 setup_dev.bat / run setup_dev.bat first' }
Write-Host '=== 打包 exe / building exe ===' -ForegroundColor Cyan
& $Py -m pip install --upgrade pyinstaller --quiet
Push-Location $Root
try {
  & $Py -m PyInstaller packaging\AIUsageWidget.spec --noconfirm --clean --distpath dist --workpath build
} finally { Pop-Location }
$exe = Join-Path $Root 'dist\AIUsageWidget\AIUsageWidget.exe'
if (Test-Path $exe) {
  Write-Host "完成 / done: $exe" -ForegroundColor Green
  Write-Host '注意：运行 exe 前先退出正在运行的源码版小组件（同一时间只能运行一个）。'
} else { throw '打包失败 / build failed' }
