# Contributing

Issues and pull requests are welcome. See [README.md](README.md) for project setup.

## Send a pull request

1. Fork the repository on GitHub.
2. Clone your fork and create a branch from `main` for one change.
3. Make the change and add tests for behavior changes.
4. Run the checks below.
5. Push the branch to your fork and open a pull request against this repository's `main` branch.
6. Describe the change and test results. Include a screenshot for UI changes.
7. Address review comments and wait for the maintainer to merge the pull request.

Contributors use forks. Only the maintainer merges changes into this repository.

## Checks

[CI](.github/workflows/test.yml) runs two checks on pull requests and pushes to `main`:

| Check | Environment | Commands |
| --- | --- | --- |
| `server` | Linux, Python 3.12, tmux | Python unittest discovery. |
| `ui` | Node.js 22, npm | Install locked dependencies, check TypeScript, run ESLint, and build the UI. |

Use Linux with Python 3.12, tmux, Node.js 22, and npm to run the same checks locally.
Run each command from the repository root:

```bash
(cd server && python3 -m unittest discover -q)
(cd ui && npm ci && npx tsc -b && npx eslint . && npm run build)
```

If you run a suite with `timeout`, add `-k 10s` so a leaked child cannot keep it alive after SIGTERM.

Fork pull requests need no secrets for CI. First-time contributors may need the maintainer to approve the workflow run.

## Change rules

- Keep one change per pull request.
- Add tests for behavior changes. Use mocks for UI writes because they control real terminals.
- Include a screenshot for UI changes. Remove private data before you attach it.
- Keep secrets and personal data out of code, tests, fixtures, screenshots, commits, and issue reports.
- Use invented fixture data. Remove credentials, tokens, personal names, personal paths, and private host names from samples.
- Update docs when behavior or setup changes.
- Keep the Python server standard-library only. Isolate optional extras so the core server works without them.
- Keep React UI changes in `ui/`; the production build goes to `ui/dist`.
- Check Claude Code, Codex, and Antigravity behavior when you change an agent adapter.
- For parser bugs, include the agent version and relevant `tmux capture-pane -p` text. Remove private data.
- Keep server samples in `server/test_fixtures/` or the relevant fixture folder.
- Tests use fixtures, mocks, and isolated tmux sessions; they need no live agent.
- Restart the backend after Python or legacy UI changes. Legacy assets load at startup.

See [AGENTS.md](AGENTS.md) for input safety and architecture rules.

## Releases

The maintainer creates release tags. Users should install only tagged releases.
The `main` branch contains development work; a merge into `main` is not a release.
See the [maintainer checklist](docs/MAINTAINING.md) for review rules and the release procedure placeholder.
