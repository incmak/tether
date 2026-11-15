# Self-contained UI tests

## Change

- Pin `playwright-core` at `1.56.1` in the UI lockfile. The machine's npm cutoff rejected `1.65.0`.
- Browser tests use `browser_test_helper.cjs`. They no longer need `PW_CORE` or a hand-set endpoint.
- Discovery checks `TETHER_CDP_URL`, then Tether's config, then local headless Chromium.
- Config paths follow `server/paths.py:CONFIG`. Endpoint construction follows `server/browser_cdp.py:Proxy.url`.
- Config overrides: `MOBILE_TMUX_CONFIG`, then `MOBILE_TMUX_HOME`, then the user's home.
- A configured shared browser must connect. Failure does not start another browser or expose its bearer token.
- With no shared config, try the installed Playwright browser, system Chrome, and Chromium on PATH.
- Every path prints its source. Missing browsers cause failure with an install command. Browser checks never skip.
- `npm run test:ui` builds current sources, runs every `ui/test_*.cjs`, and prints file pass/fail counts.
- The runner continues after failures. Each child has a ten-minute timeout. Empty selections fail.
- `--unit` and `--browser` select CI subsets. Browser files import `getBrowser` from the shared helper.
- Server browser tests prefer the pinned `ui/node_modules/playwright-core`. Explicit `NODE_PATH` remains a supported override.
- The server browser tests no longer search the temporary npx cache.
- The New Task voice harness now fixtures the new `task-models` read separately from task submissions.

## Sweep decisions

Sweep scope at `724bc70`: all 33 UI test files and all 122 server test files. Search covered environment reads, skip decorators,
`skipTest`, early returns, and successful process exits. Python AST checks confirmed skip calls.
The two browser files are the only UI tests with external environment settings in that snapshot.
The concurrent cold-load session then added a browser test and gzip server tests. Its browser test now uses the shared helper.

| File or group | Finding | Decision |
| --- | --- | --- |
| `ui/test_agent_map.cjs` | Required endpoint and `PW_CORE`; optional `DIST`, `SHOTS` | Use shared helper. Keep build/screenshot overrides with defaults. |
| `ui/test_overlay_regression.cjs` | Required endpoint and `PW_CORE`; optional `DIST`, `SHOTS` | Use shared helper. Keep build/screenshot overrides with defaults. |
| Other 31 `ui/test_*.cjs` | No external env prerequisite or silent success skip | Keep. Mock returns and process failure handlers are test logic. |
| `server/test_browser_device.py` | `NODE_PATH` and temporary npx lookup; skip without Chromium, Node, or Playwright | Prefer pinned UI install. Keep explicit optional prerequisite skip in server-only runs. |
| `server/test_mac_browser.py:234,387,391` | Chromium, Node, and Playwright prerequisite skips | Keep optional integration skips; use the pinned dependency via shared lookup. |
| `server/test_signed_access.py:584` | Chrome, Xvfb, Node, and Playwright prerequisite skip | Keep optional integration skip; use the pinned dependency via shared lookup. |
| `server/test_browser.py:408,410` | `TETHER_REAL_BROWSER_TEST=1` opt-in and Chromium check | Keep explicit opt-in for the low-level process/pipe smoke test. This is separate from UI coverage. |
| `server/test_files.py:436` | Git prerequisite skip | Keep; exercises a real Git repository. |
| `server/test_friday_agent.py:556` | Pipecat prerequisite skip | Keep; optional voice environment. |
| `server/test_groups.py:182` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_h264.py:210` | ffmpeg prerequisite skip | Keep; optional encoder integration. |
| `server/test_narrow_parsing.py:344` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_pane_zoom.py:93` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_procinfo.py:50,60` | Linux-only checks | Keep platform guard for Linux process clocks. |
| `server/test_project_launch.py:149` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_prompt_actions.py:390` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_session_controls.py:1372,1392,1406` | pywebpush prerequisite skips | Keep; optional push package. |
| `server/test_side_shells.py:103` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_signed_browser.py:231` | Chrome and virtual display prerequisite skip | Keep; optional signed-browser integration. |
| `server/test_tmux_merge.py:18` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_tmux_names.py:14` | tmux prerequisite skip | Keep; CI installs tmux. |
| `server/test_updates.py:148,556` | Banner harness skips without Node and UI dependencies | Keep for the Python-only job; run locally after `npm ci`. |
| Other server env references | Patched env, isolated subprocess env, fixture assertions, or inherited PATH | Keep; tests supply their own values. No additional hand-set prerequisite found. |
| Concurrent `ui/test_cold_load.cjs` | Shared helper; optional DIST, RUNS and budget overrides with defaults; isolated Python fixture server | Owner migrated setup. CI detects its helper import. The additional no-env run at `88cb79c` reports PASS: desktop median 1,000 ms, phone median 2,802 ms. |
| Concurrent `server/test_chat_ui_gzip.py` | Isolated static-response tests | No external env gate or skip found. Owner verifies these tests; no external environment gate. |
| Successful early exits | Mock executables and normal fixture control flow | Keep; no additional silent suite skip found. |

## CI decision

Use the existing UI job in `.github/workflows/test.yml`, pinned to Ubuntu 24.04. Run the non-browser tests after its existing build.
Run browser tests when `ui/**` or the workflow changes. Compare the PR base or previous push commit against HEAD.
If the base is unavailable, run browser tests. Cache Chromium by OS, architecture, and UI lockfile.
Install only the Chromium headless shell and its system dependencies with the pinned CLI. No new workflow or job is needed.

Both browser tests serve a local build and mock API writes. They do not need a live Tether server.
Their local fixture servers and pages close after each run.

[GitHub billing docs](https://docs.github.com/en/billing/concepts/product-billing/github-actions) confirm that standard
runners in public repositories remain free and do not reduce included compute usage. Cache storage has separate allowances.
[Playwright browser docs](https://playwright.dev/docs/browsers) describe browser installation and system dependencies.
The installed `playwright-core` CLI also confirms `install --with-deps chromium`.

## Evidence and limits

`npm ci`, `npx tsc -b`, and `npx eslint .` pass. ESLint reports 84 existing warnings and zero errors.
The New Task voice test passes after its API fixture correction.
Runner contract checks pass for failure exit, continuation, counts, selection, timeout, invalid arguments, and empty selection.
The full server suite ran 2,450 tests in 396 seconds: one failure, one cleanup error, and 59 explicit skips.
The backlog name failure is already tracked as B19. The Share API cleanup error is outside this change.
Targeted server run: 50 tests in 37 seconds, 49 pass and only the known B19 test fails.
This run includes all three changed browser modules and both original full-suite failures.
The Share API test passes on retry; its teardown race is tracked as B22.
The full UI run uses a clean detached checkout at `724bc70`.
Command: `npm ci --prefer-offline && env -u TETHER_CDP_URL -u PW_CORE -u DIST -u SHOTS npm run test:ui && npx eslint .`.
Result: **33 passed, 0 failed, 33 total**. Both browser tests report shared Chromium via Tether browser config.
Agent Map passes at 390 and 1280 pixels. Overlay regression passes 193 checks across all 48 cases.
TypeScript and Vite build pass. The clean checkout ESLint passes with zero errors and 84 existing warnings. Implementation commits: `88046ff`, `724bc70`.
Coordination: browser test owners committed their changes in `3812d43` and `8259316` before setup migration. The live shared-browser discovery smoke passes without `TETHER_CDP_URL` or `PW_CORE`.
Mocked helper checks cover config precedence, invalid config, shared connection, token redaction, local fallback, and missing browsers.
Actual local Chromium launch is not checked in this session: Tether already supplies the shared browser.
Hosted CI at `70b7c13` passed build and unit checks, then failed browser installation.
`ubuntu-latest` resolved to Ubuntu 26.04. Playwright `1.56.1` does not support that distribution.
Commit `88cb79c` pins only the UI job to Ubuntu 24.04. The installed Playwright platform and dependency tables support it.
That run was superseded by other sessions' pushes. Hosted browser coverage remains unverified.
The deployment build advances to `70b7c13` to preserve the newer Automatic setting.
That clean build passes. All 31 non-browser files pass without overrides. Settings/preferences ESLint passes.
The frontend build inputs are identical through `88cb79c`; a Git diff confirms this.
The atomic UI install publishes 2,008 files and retains prior chunks.
The served entry and both assets match the clean build. Entry SHA256 prefix: `1ea7f432f5f0c790`.
No backend restart was needed for this task. The earlier full no-env result remains scoped to `724bc70`.
The cold-load test landed in `c7456ad`, so another no-env run checked all 34 files at `88cb79c`.
It reported 33 passes and one failure: the final overlay case lost its browser connection.
The backend owner confirmed a restart at 13:18:45 IST. The service start timestamp agrees.
No automatic retry or silent skip hid that failure.
After coordination, the exact no-env npm command now checks all 34 files at `759323a` in the clean checkout.
Result: **34 passed, 0 failed, 34 total**. The exact npm command exits zero.
All three browser tests discover Tether through its config without browser/build overrides.
Cold-load medians: desktop 808 ms and phone 2,664 ms. Overlay: 48 cases, 193 checks, zero failures.
Evidence: `.release-work/ui-tests-self-contained/final-ui-tests.log`.

Server CI also reported separate failures: missing UI dependencies in its Python-only job and real Chromium context errors.
The release session fixed the install order in `c92d078`.
[Hosted run at 759323a](https://github.com/incmak/tether/actions/runs/42982219242) passes the UI job.
Its path filter skips browser installation and browser tests because that push changes only server files.
The server job runs 2,488 tests with no errors, 63 explicit skips, and one failure: project guidance is 3,017 characters.
That remaining guidance-length failure belongs to the release session and the existing B18 topic. The owner received the exact evidence.
The earlier real Chromium context errors no longer appear in that run.

## Requirement status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Exact pinned dependency | Done | `ui/package.json` and lockfile: `playwright-core` 1.56.1 |
| Shared discovery and clear failures | Done | Three real browser tests use config discovery; mocked fallback and error checks pass |
| One complete runner and count | Done | Exact no-env `npm run test:ui`: 34 passed, 0 failed |
| Environment and skip sweep | Done | Decision table above; explicit optional server skips retained |
| Lightweight CI | Done | Existing job; unit tests always; browser path filter, cache, and supported Ubuntu image |
| Hosted local-browser path | Unverified | Latest completed UI job path-skipped browsers; final push will exercise the UI path |
| TypeScript and ESLint | Done | Clean build passes; ESLint has zero errors and 84 existing warnings |
| Scoped commits and push | Done | `88046ff`, `724bc70`, `88cb79c` on main; no attribution footer in these commits |
| Internal deployment | Done | Atomic install; served entry and both assets match the clean build |
| Notes and coordination | Done | This note; project-note copy; owner messages with final results |

## Processes and retained evidence

All shell jobs started for this task have ended. No agent or wait loop was started.
All fixture servers, browser pages, and child processes from the successful suite closed.
The detached checkout remains in `.release-work/ui-tests-self-contained/checkout` at `759323a`.
It retains the build and screenshot evidence. No process runs from that checkout.
GitHub runs triggered by pushes are remote CI jobs; their status remains visible on GitHub.

## Shared browser in a container (B88, 2026-11-15)

- Since B89 the shared browser runs in a container. Its `127.0.0.1` is the container, not this host, on purpose.
- Rule: a test page must not load the test's local server through the browser network. In a `page.route` handler, use
  `serveLocal(route)` from `browser_test_helper.cjs`, not `route.continue()`. It fetches from the test process.
- A test that measures this host's real network (timing, bytes, compression) calls `getBrowser({ local: true })`.
  Today only `test_cold_load.cjs` does this.
- `run_ui_tests.cjs` finds browser tests by `const { getBrowser, ... } = require('./browser_test_helper.cjs')`.
