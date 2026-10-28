# 更新日志 · Changelog

每个版本的说明会自动出现在 GitHub Release 页面上。
Each section below becomes the notes of the matching GitHub release.

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
