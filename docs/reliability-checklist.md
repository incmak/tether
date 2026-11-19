# Reliability checklist

Each row is a failure that happened once. A row has a diff trigger and a check.
When a diff hits a row, the review request lists the row, and the reviewer confirms the check.

- Print the rows a diff hits: `python3 scripts/checklist.py` (working tree against `origin/main`), or
  `python3 scripts/checklist.py --head BRANCH`.
- Check this file after an edit: `python3 scripts/checklist.py --validate --file docs/reliability-checklist.md`.
- Reviews read this file from `origin/main`. A new row applies after it is merged.

## Row format

`Paths` and `Text` are Python regular expressions. `Paths` matches a changed path. `Text` matches an added line.
A row needs at least one of them. With both, the added line must be in a file that `Paths` matches.
`Incident` names a backlog item, a commit or a note (notes are in the project notes folder, not in this repository).

Add a row after an incident when a diff could have shown the risk. Keep the trigger narrow: a row that hits every
diff is ignored.

## Rows

## R1: `tmux kill-server` only on a private socket
- Paths: `\.(py|sh|cjs|js|ts|md)$`
- Text: `kill-server`
- Check: The same command names a private socket (`-S` or `-L`). No test, script or instruction stops the tmux server that holds the user's tabs: that ends every agent in it.
- Incident: auto-resume work, commit 62ebf51; note auto-resume.md ("Do not run kill-server on the real tmux server")

## R2: Links that leave Tether stay in the app scope
- Paths: `^server/.*\.py$|^integrations/|^ui/public/sw\.js$`
- Text: `/mobile/#session|/mobile/\?|/chat/open/|/chat/\?session=`
- Check: A link sent to another app (Teams, mail, an urgent ping) points inside `/chat/` (`/chat/open/N` for a pane). A `/mobile/...` link opens the browser, not the installed app. Test the link from another app on an Android phone with the app installed.
- Incident: B25, commits 4e8a723 and 2f19df5; note android-links-open-tether-app.md

## R3: Worker tabs start without a trust prompt
- Paths: `^server/(project_launch|delegate|agent_tabs|worktrees|auto_resume)\.py$`
- Text: `--add-dir|--settings|additionalDirectories|claude_args|codex_args`
- Check: Start one worker tab in a new worktree on the current agent version. It reaches its prompt with no trust question and can read the project folder.
- Incident: B188, commit 5d59421; note b188-trust-prompt.md

## R4: Share tests drain preview jobs before teardown
- Paths: `^server/test_share.*\.py$`
- Check: A test that finishes an upload or lists files waits for the preview queue (`drain_previews()`) before it removes the store folder. Run the changed test class 10 times.
- Incident: B22, commit 68e397d; note b22-share-test-teardown.md

## R5: No build writes into the live UI folder
- Paths: `^ui/(vite\.config\.ts|package\.json)$|^scripts/|^\.github/|^README\.md$|^AGENTS\.md$`
- Text: `outDir|emptyOutDir|ui/dist|npm run build|vite build`
- Check: A build goes to a separate folder in a clean checkout. Only `scripts/deploy-ui.py` writes `ui/dist`. A build in the main checkout is live at once.
- Incident: 2026-11-05 a build during a merge broke the live UI; guard in commit cc7ae50

## R6: The deploy guard still refuses unsafe installs
- Paths: `^scripts/(deploy-ui|verification)\.py$|^ui/vite\.config\.ts$`
- Check: Run `python3 -m unittest discover -s scripts`. The install still refuses a build that drops live commits, a build from uncommitted changes, a second deploy at the same time, and a missing stamp. Try `--dry-run` with a build from an older commit.
- Incident: B92, commit 30af7fe; 2026-11-07 an install from origin/main removed the deployed commit 2c13eb2

## R7: No personal paths or private host names
- Text: `/(home|Users)/[A-Za-z]`
- Check: The added line has no personal path. Use `paths.py`, an environment override, or a neutral path such as `/srv/project` in tests. The pre-push check blocks the push otherwise, and a fix after other commits needs a history rewrite.
- Incident: 2026-11-14 push blocked by commit 048b0da; note push-blocked-048b0da.md

## R8: Screen readers refuse a screen they do not know
- Paths: `^server/(footer|terminal_input|model_picker|terminal_replies|reply_preview|quota_notices|login_flow|shortcuts)\.py$`
- Check: The reader refuses when the screen does not match; it never guesses or presses keys on an unverified row. A captured screen for each agent version is in `server/fixtures/` with a test. Test each screen-driven write live on a throwaway tab, also in a narrow pane.
- Incident: note screen-parsing-root-cause.md (2026-11-03); commits 67d3117 and b31ba9e

## R9: A failed lazy chunk shows the crash screen, not a blank page
- Paths: `^ui/src/.*\.tsx?$`
- Text: `\blazy\(|\bimport\(|CrashScreen|staleChunk|vite:preloadError`
- Check: `CrashScreen` still wraps the app and `staleChunk` still reloads on `vite:preloadError`. Run `ui/test_crash_screen.cjs` against the build. The deploy keeps the old chunks.
- Incident: 2026-11-12 blank page after reload; commit 1456eaa; note blank-page-crash-screen.md

## R10: Only the user restarts a paused review
- Paths: `^server/(review_links|agent_tabs|notification_priority)\.py$`
- Text: `rounds|paused|restart|agent_prompt|turn_source`
- Check: A background turn or a prompt from another agent does not reset the round counts or remove the pause. The third failed review goes to the user. Run `test_review_links`.
- Incident: B189, commit 0d540df (a review ran 14 rounds)

## R11: Server tests use private state and store reads leave files unchanged
- Paths: ^server/(paths|unittest_environment|review_links)\.py$
- Check: Run test_xplat_paths.TestStoreIsolation and the legacy store read and backup test in test_review_links. Each store path stays outside the account home. A read leaves the original file unchanged. The first write preserves the original bytes in a rollback copy.
- Incident: B278, 2026-11-18, two test runs rewrote the live review store during migration

## R12: UI tests never default to a dated release folder
- Paths: ^ui/.*\.cjs$
- Text: release-work/20
- Check: The test gets its screenshot folder from `shotsDir()` in `ui/browser_test_helper.cjs`. Without `SHOTS` it writes to a scratch folder. Only `SHOTS` or an explicit flag such as `verify.cjs --shots` points a run at a dated `.release-work` folder.
- Incident: B219, 2026-11-17, a run of test_responsive_chat_ui.cjs replaced the 2026-11-16 release screenshots

## R13: A review checks the worktree named in its request
- Paths: `^server/(server|review_links)\.py$`
- Text: `\breview_(folder|gate_records)\b|\b_review_entry\b`
- Check: Run `test_review_scope`. A worker that commits in another worktree gets a review of that worktree. The approval checks its HEAD and status even if a later gate record names a different folder.
- Incident: B363, 2026-11-19, reviews named the worker tab's start folder after the worker committed in another worktree.
