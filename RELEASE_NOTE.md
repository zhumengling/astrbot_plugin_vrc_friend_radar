# Release Note

## Summary
This update significantly enhances the VRChat friend radar plugin with better friend presence monitoring, co-room awareness, daily summaries, hot world insights, login flow improvements, and overall stability fixes.

## Highlights

### 1. Friend monitoring enhancements
- Added refined joinability status display.
- Updated joinability rules:
  - Public / Friends+ => 可加入
  - Invite+ / Invite Only => 不可加入
- Applied the new status consistently across friend list, alerts, hot world statistics, and reports.

### 2. Co-room reminders
- Added co-room aggregation reminder when multiple monitored friends are in the same instance/world.
- Added independent co-room reminder interval control.
- Added minimum member threshold and optional "joinable only" reminder mode.
- Added `/vrc同房情况` for current co-room overview.

### 3. Daily summary report
- Added optional daily summary report with configurable send time.
- Report includes:
  - Daily event overview
  - Active friends ranking
  - Hot worlds ranking
  - Recommended world with image
- Daily task time supports generic default (`daily_task_time`) with report-specific override (`daily_report_time`).

### 4. Hot world statistics and recommendation
- Added `/vrc热门世界` command.
- Hot world statistics and daily report now use the scope of all friends who were online today.
- Added recommended world image support in reports.

### 5. AI translation for world description
- When a recommended world's description is not Chinese, the plugin now calls AstrBot AI capability to translate it.
- Added translation cache and automatic cache trimming strategy.
- Translation fallback keeps original text if AI translation fails.

### 6. Login flow improvement
- After login succeeds:
  - Automatically sync friends once
  - In private chat, automatically return one online-friends summary
- Covered both direct login success and 2FA verification success paths.

### 7. Config and command list synchronization
- `notify_group_ids` and command-managed notify groups are synchronized.
- `watch_friend_ids` and command-managed watch friends are synchronized.
- Startup reconciliation uses merge + dedupe strategy to avoid losing user data.

### 8. Stability and cleanup
- Fixed several logic inconsistencies introduced during iterative updates.
- Improved daily statistics time-window correctness.
- Cleaned private runtime cache artifacts before publication.
- Added/updated ignore rules for cache, DB, session, and temp files.

## Suggested Git Commit Message
```text
feat: enhance VRChat friend radar with daily reports, hot worlds and coroom alerts
```

## Notes
- Monitoring alerts still only apply to monitored friends.
- Daily report and hot world statistics now use all friends who were online today.
- Runtime-generated private cache files were cleaned before packaging for GitHub.
