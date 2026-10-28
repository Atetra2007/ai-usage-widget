$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host ''
Write-Host '=== 上传到 GitHub / publish to GitHub ===' -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw '没找到 git，请先安装 Git for Windows' }

if (-not (git config user.name)) {
  $n = Read-Host '还没设置 git 用户名，请输入你的 GitHub 用户名'
  git config --global user.name $n
}
if (-not (git config user.email)) {
  $e = Read-Host '请输入你的 GitHub 邮箱（或 GitHub 提供的 noreply 邮箱）'
  git config --global user.email $e
}

$repo = Read-Host '粘贴 GitHub 空仓库地址（例如 https://github.com/你的用户名/ai-usage-widget.git）'
if (-not $repo) { throw '没有输入仓库地址' }

$ver = (Select-String -Path 'app\main.py' -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
if (-not (Test-Path '.git')) { git init -b main | Out-Null }
git add -A
$msg = "AI Usage Widget v$ver`n`nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { git commit -m $msg | Out-Null }
if (git remote | Select-String -SimpleMatch 'origin') { git remote set-url origin $repo } else { git remote add origin $repo }

Write-Host '推送代码（第一次会弹出浏览器让你登录 GitHub）...'
git push -u origin main
if ($LASTEXITCODE -ne 0) { throw '推送失败，请截图发给 Claude' }

$tag = "v$ver"
if (-not (git tag --list $tag)) { git tag $tag }
git push origin $tag
Write-Host ''
Write-Host "完成。GitHub Actions 会自动打包 $tag，几分钟后在仓库的 Releases 页面就能下载 zip。" -ForegroundColor Green
