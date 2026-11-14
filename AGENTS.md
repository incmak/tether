# Coding guide

Tether is a browser chat UI for Claude Code, Codex, and Antigravity in tmux, for desktop, tablet, and phone.

## Layout and checks

- `server/`: Python standard-library server, agent adapters, fixtures, and tests.
- `ui/`: React/Vite UI. Production builds go to `ui/dist`, served at `/chat/`.
- `server/index.html` and `server/app.js`: legacy UI, served at `/mobile/chat/`.
- Server checks: `cd server && python3 -m unittest discover -q`.
- UI checks: `cd ui && npm ci && npx tsc -b && npx eslint .`. Build from a clean checkout before deployment.
- Deployment and configuration: see [README.md](README.md).

## Rules and architecture

- Keep secrets, personal paths, and private host names out of the repository.
- Use `server/paths.py` and environment overrides for agent and state paths.
- Keep the core server free of third-party Python dependencies.
- Chat comes from agent transcripts. Pending prompts come from the tmux pane.
- Auth lives in `server/auth.py`. Preserve cookie checks, Origin checks, and the `X-Codex-Mobile` header for writes.
- Bind writes to the selected pane and conversation. Reject stale identities and preserve existing terminal drafts.
- Input checks live in `terminal_input.py`, `terminal_replies.py`, `model_picker.py`, and `shortcuts.py`, under `server/`.
- For agent parser changes, inspect record types and check behavior across all three agents.
- Tests use fixtures and isolated or mocked processes. Use mocks for UI writes; they control real terminals.
- Restart the backend after Python or legacy UI changes. Legacy assets load at startup; React assets come from `ui/dist`.

## Terminal screens change with each agent update

Claude Code, Codex and Antigravity redraw their screens between versions. Code that reads or drives a screen must survive that.

1. Prefer a channel that does not depend on the screen: the process, transcript, session and task files. Use the screen only when no such channel exists.
2. Code that reads a screen must refuse when the screen does not match what it expects. It must never guess, press keys on an unverified row, or act on a partial match.
3. Keep real captured screens for each agent version in `server/fixtures/`, for example `server/fixtures/claude_2.1.325_tasks/`. Test the parsers against them. Remove private paths from captures before you commit them.
4. After each agent update, test each screen-driven write live on a throwaway tab, for example Stop on agents, shells and monitors. Add fixtures for any screen that changed.
5. When a refusal may come from a new version, say so in the error and name the version.

## Internal deployment and public release

- Internal deployment updates Mak's running Tether. Complete authorized work through checks, a scoped commit, and internal deployment automatically.
- Routine commits, pushes, and internal deployments need no additional approval from Mak.
- The session that completes the work owns deployment. Use another session only when Mak explicitly assigns it.
- Before committing or deploying, inspect shared files and active sessions for overlapping changes.
- Contact the sessions that own conflicting changes. Agree on file or hunk ownership, consolidate compatible changes, and verify the result.
- Resolve routine conflicts between sessions. Ask Mak only when the conflict requires a product decision or destructive action.
- Preserve unrelated work. Build the UI from the selected commit in a clean checkout, then install the output atomically.
- Install with `python3 scripts/deploy-ui.py BUILD_DIR ui/dist`. It preserves old chunks and publishes the entry page last.
- Verify the served entry page and deployed behavior after installation.
- The backend loads the shared working tree on restart. Coordinate pending server edits before a restart.
- A public release publishes an update to all users. Publish release tags, packages, or update channels only when Mak explicitly requests a release.
