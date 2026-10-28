$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host ''
Write-Host '=== 发布到 GitHub / publish to GitHub ===' -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw '没找到 git，请先安装 Git for Windows' }

if (-not (git config user.name)) {
  $n = Read-Host '还没设置 git 用户名，请输入你的 GitHub 用户名'
  git config --global user.name $n
}
if (-not (git config user.email)) {
  $e = Read-Host '请输入你的 GitHub 邮箱（或 GitHub 提供的 noreply 邮箱）'
  git config --global user.email $e
}

$ver = (Select-String -Path 'app\main.py' -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
$tag = "v$ver"
Write-Host "版本 / version: $tag"
if (-not (Select-String -Path 'CHANGELOG.md' -Pattern ("^## \[?v?" + [regex]::Escape($ver) + "\]?(\s|$)") -Quiet)) {
  Write-Host "提示：CHANGELOG.md 里还没有 $tag 的更新说明，Release 页面只会有下载链接。" -ForegroundColor Yellow
}

if (-not (Test-Path '.git')) { git init -b main | Out-Null }
if (-not (git remote | Select-String -SimpleMatch 'origin')) {
  $repo = Read-Host '粘贴 GitHub 空仓库地址（例如 https://github.com/你的用户名/ai-usage-widget.git）'
  if (-not $repo) { throw '没有输入仓库地址' }
  git remote add origin $repo
}
$remote = git remote get-url origin
Write-Host "仓库 / repo: $remote"

git add -A
git diff --cached --quiet
$dirty = ($LASTEXITCODE -ne 0)

# 同一个版本号只发布一次：有新改动却没改版本号时提醒，而不是悄悄只推代码
if (git tag --list $tag) {
  $same = ((git rev-list -n 1 $tag) -eq (git rev-parse HEAD))
  if ($dirty -or -not $same) {
    git reset -q
    throw "$tag 已经发布过了。请先把 app\main.py 里的 VERSION 改成新版本号，并在 CHANGELOG.md 顶部写上更新内容，再运行一次。"
  }
}

if ($dirty) {
  $msg = "AI Usage Widget $tag`n`nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  git commit -q -m $msg
  if ($LASTEXITCODE -ne 0) { throw '提交失败，请截图发给 Claude' }
}

Write-Host '推送代码（第一次会弹出浏览器让你登录 GitHub）...'
git push -u origin main
if ($LASTEXITCODE -ne 0) { throw '推送失败，请截图发给 Claude' }

if (-not (git tag --list $tag)) { git tag $tag }
git push origin $tag
if ($LASTEXITCODE -ne 0) { throw '推送版本标签失败，请截图发给 Claude' }

$web = ($remote -replace '\.git$', '')
Write-Host ''
Write-Host "完成。GitHub Actions 正在打包 $tag，大约 2 分钟后会出现在：" -ForegroundColor Green
Write-Host "  $web/releases"
if ($web -like 'https://*') { Start-Process "$web/actions" }
