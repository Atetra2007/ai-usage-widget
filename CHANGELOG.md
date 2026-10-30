# 更新日志 · Changelog

每个版本的说明会自动出现在 GitHub Release 页面上。
Each section below becomes the notes of the matching GitHub release.

## 未发布 · Unreleased

- Codex 支持多个账号：以前切换账号后，小组件只取日志里最新的一条额度，两个号的数据会互相覆盖、来回跳。现在读 `auth.json` 认出当前登录的号，用额度窗口的重置时间区分各个号的记录（切号前开的会话还在跑也不会算错），每个号分开显示，当前号快用完时提示切到还有余量的号。
- Codex 额度改为实时查询（和 Codex `/status` 同一个接口，只读，不刷新令牌）：切号后马上显示新号的额度，不用等它发一次请求。会自动找 Cockpit Tools、cc-switch、codex-switch 等切换工具保存的其他账号一起查询；也可以在 `settings.json` 的 `codex_auth_files` 里指定位置，`codex_labels` 给账号起名。设置里「Codex 实时额度」可以关掉。
- Multiple Codex accounts: switching accounts used to make the widget jump between them, because it only took the newest limit entry from the logs. It now reads `auth.json` to know the signed-in account, tells accounts apart by their limit reset times (even when a session started before the switch is still running), shows each account separately, and suggests switching when the current one is almost out.
- Codex usage is now queried live (the same read-only endpoint as Codex's `/status`; tokens are never refreshed), so a newly signed-in account shows its usage right away. Accounts saved by switchers such as Cockpit Tools, cc-switch or codex-switch are found and queried too; set `codex_auth_files` in `settings.json` if yours stores them elsewhere, and name accounts with `codex_labels`. Turn it off with "Live Codex usage" in settings.
- Claude 新增「今日用掉的额度」：网页版、Cowork、手机端的 Claude 不在本机留日志，官方接口也只给百分比，所以按额度百分比的增量统计今天用了周额度的多少（例如「今日 周额度 +8%」）。本机 Claude Code 的 token 照常显示。
- 也会读取 Claude 桌面版在本机运行的会话日志。
- Claude now shows how much of your limit you used today (e.g. `today +8% of week`). Claude on the web, in Cowork and on mobile leaves no local logs and the usage API only reports percentages, so this is tracked from the quota changes. Local Claude Code tokens are still shown.
- Also reads session logs from Claude desktop sessions that run on this PC.
- 修复开机自启后小组件有时变成不透明、要退出重开才正常的问题：登录后先等桌面和显卡稳定（默认 40 秒，`settings.json` 里的 `boot_delay` 可改）再显示窗口；显示器 / 显卡驱动重置后自动重新应用透明；WebView2 掉到软件渲染时自动重启（最多连续 2 次）。
- Fixed the widget sometimes turning opaque after starting with Windows until it was reopened: it now waits for the desktop and GPU to settle after login (40 s by default, `boot_delay` in `settings.json`), re-applies transparency after display or driver resets, and restarts itself if WebView2 falls back to software rendering (at most twice in a row).

## v1.0.3 — 2026-10-28

**改进**

- 显存精确到小数点后一位，例如「显存 27.4/32.0G」。
- 多显示器：重启后回到上次所在的那块屏幕，不再被拉回主屏。
- 只有每周额度的 Codex 套餐会正确显示为「每周额度」，不再被当成 5 小时窗口；同时兼容旧版 Codex 的重置时间格式。

**修复**

- 修复多个采集线程同时读写缓存时偶尔保存失败的问题。
- 缓存会清理已删除日志的记录，`cache.json` 不会越来越大（每日用量历史不受影响）。
- `settings.json` 损坏时，会备份为 `settings.json.bad` 并重新识别数据来源，不会再什么都不显示。
- 启动时 WebView2 还没就绪不再往 `error.log` 里写多余的报错。

**English**

- VRAM is shown with one decimal place, e.g. `VRAM 27.4/32.0G`.
- Multi-monitor: the widget reopens on the screen it was last on instead of jumping back to the primary display.
- Codex weekly-only plans are shown as a weekly limit instead of being mislabeled as the 5-hour window. Older Codex reset-time formats are supported too.
- Fixed occasional cache save errors when collectors ran at the same time. The cache now drops entries for deleted logs.
- A corrupt `settings.json` is backed up to `settings.json.bad` and sources are detected again, so the widget no longer shows nothing.
- No more spurious startup errors in `error.log` while WebView2 is still loading.

## v1.0.2 — 2026-10-28

首个公开版本 · First public release.

- Claude / Codex 的 5 小时和每周额度、重置倒计时、按时间均匀消耗的参考线和用完时间预测，以及今日和累计 token。
- 本地模型（Bionic / LM Studio）：实时生成速度、90 秒速度曲线、MTP 接受率、显存，以及今日和累计 token。
- 首次启动自动识别已安装的工具；智能提醒；三种透明样式；贴边吸附；全屏时自动隐藏；开机自启；中英双语。
- Claude / Codex 5-hour and weekly limits with reset countdown, pace marker and run-out forecast, plus tokens today and in total.
- Local model (Bionic / LM Studio): live speed, 90-second chart, MTP acceptance, VRAM, tokens today and in total.
- Auto-detects your tools on first launch; smart alerts; three transparent styles; edge snapping; hides over fullscreen apps; start with Windows; English and Chinese UI.
