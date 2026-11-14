# Tether

Tether is a chat UI for coding agents in tmux: Claude Code, Codex, and Antigravity (`agy`).
Run agents on your Linux machine, then read their chat, approve requests, answer questions, and send instructions from any browser: desktop, tablet, or phone.
The agent stays in tmux when you leave your desk.

![Tether demo: a live agent turn, split view, the built-in browser, and Friday Live mode](docs/tether-demo.gif)

- Read chat from the agent's transcript, with tool actions collapsed.
- Answer approval and folder-trust prompts with their real options.
- Switch between agent sessions and shell tabs.
- Attach photos and files, select models, and view usage reports.
- Use `!command` for shell commands in the session's folder.
- Keep one backlog across all sessions that you, Claude Code and Codex can all add to.
- Use dictation and push notifications where supported.

The server runs on your machine. There is no Tether cloud account or relay.
Agent providers and optional push services retain their own network and data policies.
Tether is a front end for agent and shell panes, rather than a general tmux client with windows and layouts.

New here? Follow the [setup guide](https://incmak.github.io/tether/).

## Repository layout

| Directory | Purpose |
| --- | --- |
| `server/` | Python standard-library server, agent adapters, tests, and legacy UI. |
| `ui/` | React/Vite chat UI. The server serves `ui/dist` at `/chat/`. |

[Upstream screenshots and demo](server/docs/) show the legacy UI, which remains available at `/mobile/chat/`.
They do not show the current React UI.

## Requirements

- Linux, or macOS with Homebrew Python. On macOS, the installer uses a LaunchAgent. Testing covers Claude Code on one Mac.
- Python 3.11 or newer.
- Node.js 22 and npm for development UI builds. Release archives include the built UI.
- `tmux` and at least one installed agent: Claude Code, Codex CLI, or Antigravity.
- On Linux, a systemd user session for the installer. The installer needs no root.
- Optional: [`ttyd`](https://github.com/tsl0922/ttyd) for the interactive terminal, and `pywebpush` for push notifications.

The core server uses only the Python standard library. Release installs need no Node or npm.

## Set up with Claude Code or Codex

The agent offers features and remote access, shows an installer plan, then waits for your yes before apply.

**Claude Code**

1. Run `/plugin marketplace add incmak/tether` in Claude Code.
2. Run `/plugin install tether@tether` and install the plugin.
3. Type `set up tether` or `/tether:tether-setup` and follow the guided setup.

**Codex CLI**

1. Prepare a release with the bootstrap in Quick start below, or use an existing release.
2. From `~/.local/share/tether/current`, run the three copy commands in the [Codex setup skill](integrations/codex/skills/tether-setup/SKILL.md#install-this-codex-skill). They move an existing copy aside before copying.
3. Restart Codex if needed, then type `set up tether` or `$tether-setup` and follow the guided setup.

The Codex copy steps use the local skill layout; current [Codex docs](https://developers.openai.com/codex/skills) list `~/.agents/skills`.
If your CLI does not find the skill, copy the folder there instead.
See the [Codex setup skill](integrations/codex/skills/tether-setup/SKILL.md) for details.
See the [Claude Code marketplace docs](https://code.claude.com/docs/en/plugin-marketplaces) for plugin installation details.

## Quick start

The setup skills install the latest stable release by default. To prepare a release without a checkout:

```sh
curl -fL https://raw.githubusercontent.com/incmak/tether/v0.1.11/integrations/claude/tether/skills/setup/bootstrap.py -o tether-bootstrap.py
python3 tether-bootstrap.py
python3 ~/.local/share/tether/current/install.py --plan --json --no-build-ui
python3 ~/.local/share/tether/current/install.py --apply --json --no-build-ui
```

Review the plan and resolve failed checks before apply. The script uses only Python's standard library.
It downloads the release tarball and `SHA256SUMS`, checks SHA-256, and rejects unsafe archive paths and links.
It installs files into `~/.local/share/tether/releases/<version>/` and atomically points `current` at that release.
It does not start services; `install.py --apply` handles services, hooks, dependencies, and saved choices.
The installer creates `~/.local/bin/tether -> ~/.local/share/tether/current/tether` and preserves any foreign command there.
Its plan includes `cli_link` and warnings. If `~/.local/bin` is absent from PATH, add:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Start `tmux new -s work`, then run `claude`, `codex`, or `agy` inside it.
Open the printed `/chat/` URL. Sign in with the token from the printed file path.

For development, explicitly choose a git checkout. Run `python3 install.py --plan`, then `python3 install.py --apply` there.
The checkout installer builds the UI when needed.

Each Linux user runs the installer separately. It selects the first free chat port from 7683 and separate optional service ports.
Ports occupied by other users count as taken.
The units are `tether.service`, `tether-terminal.service`, and `tether-voice.service`; optional units exist only when enabled.
Existing `mobile-tmux*.service` units stay untouched unless you add `--replace-legacy`, which disables and stops them.
The old `server/deploy/install.py` entry point still works.
To run without a service, use `python3 server/server.py` after the UI build.

### Android (Termux)

1. Install [Termux from F-Droid or GitHub](https://github.com/termux/termux-app#installation), not Google Play.
2. Install the packages below. Then close and reopen Termux to start [termux-services](https://github.com/termux/termux-services).

   ```sh
   pkg install python tmux nodejs git ttyd termux-services
   ```

3. Clone the repository and run the installer:

   ```sh
   git clone https://github.com/incmak/tether.git
   cd tether
   python install.py --plan --no-voice
   python install.py --apply --no-voice
   ```

4. Run `termux-wake-lock` so Android does not stop Termux. Optionally, configure [Termux:Boot](https://github.com/termux/termux-boot) to start services after a reboot.
5. Open the URL that the installer prints in the phone browser.

Voice and automatic updates are not supported on Termux. Agent CLIs on Termux are not tested.

## Reaching it from other devices

The server binds to `127.0.0.1` by default. Anyone with access can control your agents and run commands as you.
Do not expose it directly to the internet.

Use a private network such as Tailscale, or an HTTPS reverse proxy with its own sign-in.
For proxy authentication, set `MOBILE_TMUX_AUTH=proxy` and retain the loopback bind.
See the [nginx proxy example](server/deploy/examples/nginx-auth-proxy.conf).

For a trusted LAN, `MOBILE_TMUX_HOST=0.0.0.0` allows token access over the network.
Plain HTTP sends the token and chat without encryption. Push notifications and home-screen installation need HTTPS.
On a phone or tablet, open `/chat/` and use “Add to Home Screen” where the browser supports it.

## Signing in and security

| Mode | Setting | Behavior |
| --- | --- | --- |
| `token` (default) | No setting needed | Creates an owner-only token in `~/.config/mobile-tmux/token`. Sign-in sets an HttpOnly, SameSite=Strict cookie. Failed attempts are rate-limited. |
| `proxy` | `MOBILE_TMUX_AUTH=proxy` | Trusts the reverse proxy's authentication. Refuses startup with a non-loopback bind. |

State changes also need a matching `Origin` and the custom `X-Codex-Mobile` header.
To rotate the token, remove `~/.config/mobile-tmux/token` and restart the service.
Treat the token like an SSH key. Shell commands run as the server user.
Report security problems privately; see [SECURITY.md](SECURITY.md).

Attachments use random names in the owner-only `~/.cache/mobile-tmux/uploads/` folder and expire after a week.
The agent receives the file path. It may request permission to read that folder.
The upload endpoint does not serve files back; authenticated file previews use separate checks for paths, types, and secrets.
Those checks restrict previews to supported files under your home folder and exclude credential folders and secret-like names.

`POST /mobile/api/files/save` saves UTF-8 text up to 2 MB with the same sign-in and write checks.
Send a JSON object with `path`, `text`, and `base` (the current file's lowercase SHA-256 hash); `session` resolves relative paths against the tab folder.
The response returns the new `base`. Saves preserve the file mode and reject disk changes, symlinks, secrets, and the managed Share folder.

## Office editor add-on (optional)

The add-on edits docx, xlsx, and pptx files with ONLYOFFICE Docs Community Edition in a separate Docker container.
It is off by default. The core server needs no Docker installation.
The editor runs on its own port to isolate it from Tether.
ONLYOFFICE Docs Community Edition uses the AGPL license.
See the [official Docker guide](https://helpcenter.onlyoffice.com/docs/installation/community/docker/installing/install) for Docker requirements.

1. Install and start Docker.
2. Run `python3 server/office_addon.py install` from your Tether checkout.
3. Restart Tether with `systemctl --user restart tether.service` on Linux.

If you reach Tether through Tailscale Serve, run the command that the installer prints:

```sh
tailscale serve --bg --https=<editor_port> http://127.0.0.1:<editor_port>
```

Replace `<editor_port>` with the configured editor port (7697 by default).
See the [Tailscale Serve reference](https://tailscale.com/docs/reference/tailscale-cli/serve) for these options.

On macOS, restart Tether through your LaunchAgent instead.
Run `python3 server/office_addon.py status` to check the container and its health.
Run `python3 server/office_addon.py remove` to remove the container and config.
Restart Tether after removal.

The config uses `TETHER_OFFICE_CONFIG`, or `office.json` in Tether's config folder.
The editor uses Tether sign-in. Docker file downloads and saves require signed capabilities and JWTs.
Tether blocks secret files and Share files. External edits go to a separate `(Tether edit)` copy.
Phones get the desktop editor, because Community Edition opens its mobile editor for viewing only.

## Panel browser in a container (optional)

When Docker and the `lscr.io/linuxserver/chromium` image are present, the panel browser runs in a container and streams as live video.
Tether serves the live view on its own port (7698 by default, `MOBILE_TMUX_BROWSER_LIVE_PORT`), so the page in it cannot reach Tether.
Without that port, the panel shows still pictures as before. Set `MOBILE_TMUX_BROWSER_SELKIES=0` to keep the local Chromium.

If you reach Tether through Tailscale Serve, publish the port too:

```sh
tailscale serve --bg --https=7698 http://127.0.0.1:7698
```

## Usage reports

Claude Code and Antigravity supply usage through status-line hooks.
Run `python3 install.py --apply --hooks` to add hooks where no status line exists.
For Claude Code, this also appends live state hooks without changing existing user hooks.
Session hooks supply mode, effort, tool activity, turn activity, permission waiting, and subagents to the footer.
Mode and effort last until the session ends. A differing mode line from a later screen read takes precedence.
Open tools have no age limit. Other turn state uses the screen after 30 minutes, or after a newer transcript turn.
Screen agent rows and other footer fields remain. Missing or invalid hooks use the screen as a fallback.
With `--hooks`, the installer wraps an existing Claude status line, so Tether saves the status data and the line looks the same.
It keeps a `.before-tether` backup.
Uninstall restores the original command.

For Claude Code, a standalone recorder in `~/.claude/settings.json` looks like this:

```json
{"statusLine": {"type": "command", "command": "python3 /path/to/tether/server/claude_usage.py"}}
```

Antigravity uses `~/.gemini/antigravity-cli/settings.json`, `server/antigravity_status.py`, and `"enabled": true` in that block.
Restart the agent after a hook change. Codex needs no hook.
Without hooks, chat still works, but some usage reports are unavailable.

## Configuration

1. Review changes with `python3 install.py --plan --port 7800 --host 127.0.0.1 --auth token`.
2. Apply them with `python3 install.py --apply` and the same options.
3. To remove services, review `python3 install.py --plan --uninstall`, then run `python3 install.py --apply --uninstall`.

`--json` prints a deterministic plan for tools. Plans write no files and do not start or stop services.
Saved choices live in `~/.config/tether/install.json`. Runs without options reuse them; the plan lists reused choices.
`--terminal` defaults on when ttyd exists. `--voice`, `--push`, `--images`, `--hooks`, and `--resources` default off.
Each switch accepts a `--no-` form. `--build-ui` forces a build; the default builds missing or stale `ui/dist`.
Voice uses `~/.local/share/tether/whisper-venv`; push and images share that environment or use `~/.local/share/tether/server-venv`.
`--images` installs Pillow there, to make small WebP copies of backdrop photos. Without it, Tether uses ffmpeg when it is installed, or keeps the original file.
Uninstall removes only Tether's Claude live state hooks. It keeps status lines, user hooks, tokens, dependencies, and data.
The installer prints the optional `loginctl enable-linger $USER` command for services that must survive logout.
It does not run this command; admin permission may be needed.

For manual startup, use the environment variables below.
For advanced service settings, add `Environment=NAME=value` to a systemd user override and restart the service.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOBILE_TMUX_HOST` | `127.0.0.1` | Listen address. |
| `MOBILE_TMUX_PORT` | `7683` | Listen port. |
| `MOBILE_TMUX_AUTH` | `token` | `token` or `proxy`. |
| `MOBILE_TMUX_TRUST_FORWARDED` | Off | Set to `1` only behind a local proxy that replaces `X-Forwarded-For`. Rate limits then use the forwarded client address. |
| `MOBILE_TMUX_TTYD_PORT` | `7682` | Optional interactive terminal port. |
| `MOBILE_TMUX_PUSH_SUBJECT` | `mailto:mobile-tmux@localhost` | Contact URL for push services. Set your own contact if you use push. |
| `MOBILE_TMUX_HOME` | Your home | Root for the agent folders: `.claude`, `.codex`, and `.gemini`. |
| `MOBILE_TMUX_CACHE` | `~/.cache/mobile-tmux` | Usage reports and other working data. |
| `MOBILE_TMUX_CONFIG` | `~/.config/mobile-tmux` | Access token and push keys. |
| `MOBILE_TMUX_TMUX` | Found on `PATH` | Path to the tmux executable. |
| `TMUX_CHAT_DIST` | `<repo>/ui/dist` | Override the React UI build folder. |

## Live mode workers

Live mode uses the Whisper worker for transcription and [`tether-tts.service`](server/deploy/tether-tts.service) for natural speech.
[`tether-llm.service`](server/deploy/tether-llm.service) runs llama-server with `-t 6` on `127.0.0.1:7687` for local summaries.
It uses `~/.local/share/tether/friday-models/Qwen3.5-2B-Q4_K_M.gguf` and the binary in `~/.local/share/tether/llama/`.
Summaries try the local model, then Claude Haiku, then two sentences from the reply.
Set `TETHER_LLM_URL` on the Tether server to override the local URL.

Copy the units, then enable and start them:

```bash
mkdir -p ~/.config/systemd/user
cp server/deploy/tether-llm.service server/deploy/tether-friday.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tether-llm.service tether-friday.service
```

The optional Kokoro worker uses `server/tts_worker.py` and `tether-tts.service` as the fallback voice path.
Friday uses `server/friday_agent.py` in `~/.local/share/tether/friday-venv` (Pipecat 1.15.0).
Its models live in `~/.local/share/tether/friday-models`: Parakeet v3, Smart Turn v3.2, Qwen3.5-2B, and the Pocket TTS cache in `hf/`.
Friday uses Pocket TTS's `alba` voice with offline Hugging Face access.

The two Friday units are `tether-friday.service` (worker, loopback port 7686) and `tether-llm.service` (llama-server, loopback port 7687).
The unit files live in `server/deploy/`. They require the prepared venv and models; the installer does not install these units.
`TETHER_FRIDAY_PORT`, `TETHER_LLM_URL`, and `TETHER_FRIDAY_MODELS` override the worker defaults.
`TETHER_FRIDAY_URL` sets the server's signaling proxy destination.
Set `TETHER_FRIDAY_BRAIN=gemini` for Vertex AI; `local` is the default. STT, VAD, Smart Turn, and TTS stay local.
`TETHER_FRIDAY_GCP_PROJECT` uses the ADC project if unset; `TETHER_FRIDAY_GEMINI_MODEL` defaults to `google/gemini-3.5-flash-lite`.
`TETHER_FRIDAY_GEMINI_EFFORT` defaults to `low`; `default` omits it. `TETHER_FRIDAY_VAD_STOP` defaults to 0.5 seconds with Smart Turn.
Use [`gcloud auth application-default login`](https://docs.cloud.google.com/sdk/gcloud/reference/auth/application-default/login) for credentials. The service reads optional settings from `~/.config/tether/friday.env`.
With `gemini`, the user's speech text and chat state go to Google.

The browser reaches the worker's WebRTC ports directly through host candidates, such as the host's Tailscale IP.
UDP must be open between the browser and the host. The HTTP proxy carries signaling, while WebRTC carries audio and RTVI messages.

## Host resources

Enable `--resources` in the installer, or set `TETHER_RESOURCES=1` for manual startup.
Settings → System shows host CPU, memory, disk, and cgroup v2 groups. It refreshes every five seconds while visible.
CPU starts with the second sample; group CPU shows cores used and the share of its limit or host cores.
Missing cgroup files do not stop the host summary.
If btop and the terminal feature are available, “Open btop” opens it in a separate side shell.
Glances process details appear when its local API answers at `http://127.0.0.1:61208`; other URLs are ignored.

## How it works and its limits

The server maps tmux panes to agent conversations and reads each agent's transcript files.
Pending approval and question prompts come from the pane because they are not yet in the transcript.
Before input, the server checks the agent process, conversation, pane identity, and existing draft.
See [server behavior notes](server/docs/behavior.md) for details.

- Use one input surface at a time. Terminal and phone input can race.
- Agent updates can break prompt, picker, or status-line parsing. Report the agent version and a redacted pane sample.
- Two Codex sessions in the same folder can be ambiguous; the second is read-only.
- Chat shows tool calls with expandable results, but not the agent's hidden reasoning.
- Attachments have a 20 MB limit. Text previews have a 2 MB limit.
- Browser speech, push, and installation support varies. Do not rely on offline access.

## Bugs and development

Report bugs and setup questions through [repository issues](https://github.com/incmak/tether/issues).
Include the agent version and relevant `tmux capture-pane -p -t <pane>` output with private data removed.
See [CONTRIBUTING.md](CONTRIBUTING.md) for server tests and UI checks.

## Credits

Tether is built on [Lowell Winston's mobile_tmux_public](https://github.com/LowellWinston/mobile_tmux_public), licensed under MIT.
Parts of the UI derive from [T3 Code](https://github.com/pingdotgg/t3code), also licensed under MIT.
See [third-party notices](THIRD_PARTY_NOTICES.md) for attribution and vendored libraries.
Tether is licensed under the [MIT License](LICENSE).

## Updating

Only tagged releases reach users. A push to `main` does not publish an update.
The server checks the latest stable release once per day without an authentication token.
The sidebar shows a dismissible link to release notes. Each version has its own dismissal.
Failed checks keep the last known result. `off` disables these checks and the banner.

Use Quick start or either setup skill for the first release install.
The archive contains the built UI. Services, hooks, status lines, and the CLI use the stable `current` path.
With an available CLI, `tether install` prepares the same release layout without applying the installer.
It refuses an existing `current`; use `tether update` for an established release install.
Bootstrap and `tether install` accept `--to vX.Y.Z` and paired `--archive PATH --sums PATH` for offline installs.
Keep the archive filename `tether-<version>.tar.gz`. SHA-256 and archive safety checks also apply offline.

```bash
tether update --check              # Report the latest stable release.
tether update                      # Ask before applying it.
tether update --yes                # Apply without a prompt.
tether update --to v0.2.0          # Select a published version, including an rc release.
tether rollback                    # Restore the previous release.
python3 ~/.local/share/tether/current/install.py --plan --updates auto --no-build-ui
python3 ~/.local/share/tether/current/install.py --apply --updates auto --no-build-ui
```

In the web UI, **Update now** in the sidebar runs the same verified update. It appears only on release installs.

The installer saves `--updates notify|auto|off`. The default is `notify`.
Only `auto` writes the daily `tether-update.timer` and its service.
The timer uses a randomized one-hour delay. Its service runs `tether update --auto`.
Choose `notify` or `off` to remove the timer.

The CLI verifies `SHA256SUMS` before extraction. It rejects unsafe archive paths and links.
It stages the release beside the current release, then changes `current` atomically.
It restarts Tether and checks `/chat/` for 30 seconds.
A failed install, restart, or health check restores the prior release, units, and saved choices.
A successful update keeps the active release and one rollback release.
Updates use a lock to prevent concurrent changes. They preserve chat data and agent transcripts.

A git checkout refuses release updates. Developers use `git pull`, then `python3 install.py --apply` there.
To move an existing checkout install to releases, prepare a release with bootstrap, then run:

```sh
python3 ~/.local/share/tether/current/tether migrate
# Optional: --from /path/to/checkout --to v0.1.2
# Offline: --archive /path/to/tether-0.1.2.tar.gz --sums /path/to/SHA256SUMS
```

Migration uses the checkout path and choices in `~/.config/tether/install.json` (or `TETHER_INSTALL_STATE`).
An explicit `--from` must match that saved checkout. Use the same tag/archive flags as bootstrap.
A prepared release must match the verified archive before migration can reuse it.
Migration reapplies the installer without a UI build, then checks `/chat/`.
It moves unit paths, Claude lifecycle hooks, Tether usage status lines, saved `repo`, and automatic update units to `current`.
It keeps tokens, preferences, configuration, chat data, and agent transcripts.
Failure restores checkout units, settings, and saved state, then restarts the checkout services.
The checkout stays untouched, including local changes. After success, the user can remove it.
Set `TETHER_REPO` to choose another release repository. The default is `incmak/tether`.


## Share storage

Share stores personal files under `TETHER_HOME/share` (default `~/.tether/share`). Set `TETHER_SHARE` to use another folder.
The authenticated `/mobile/api/share/` API supports uploads, downloads, thumbnails, trash, and private chat exports.
The default quota is 50 GiB, and the file limit is 2 GiB. Set `TETHER_SHARE_QUOTA_BYTES` or `TETHER_SHARE_MAX_BYTES` to override them.
Uploads require 20 GiB of free disk space after reservation. Two uploads can run at once.
Startup reconciliation purges trash after 30 days and expires incomplete uploads after 24 hours.
Trash counts against quota. Purge removes the file's thumbnails and exports; exports survive trash and restore.
Use `python3 server/agent_share_cli.py put PATH`, `list`, `stat ID`, or `path ID` for local access.
Back up the catalog and objects together while Share is idle. The Files explorer cannot change the managed Share folder.
