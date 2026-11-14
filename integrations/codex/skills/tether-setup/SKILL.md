---
name: tether-setup
description: Guide Tether setup when the user says "set up tether", "install tether", or "update tether settings".
---

# Set up Tether

## Install this Codex skill

From a prepared Tether release or an explicit developer checkout, copy this folder into the user skill directory.
If `~/.codex/skills/tether-setup` already exists, move it aside first so local edits stay:

```sh
mkdir -p ~/.codex/skills
[ -e ~/.codex/skills/tether-setup ] && mv ~/.codex/skills/tether-setup ~/.codex/skills/tether-setup.bak-$(date +%s)
cp -R integrations/codex/skills/tether-setup ~/.codex/skills/
```

Restart Codex if the skill does not appear, then type `set up tether` or `$tether-setup`.
These copy instructions target the local `~/.codex/skills` layout.
Current [Codex skill docs](https://developers.openai.com/codex/skills) list `~/.agents/skills` for user discovery.
If this CLI does not discover the copied skill, copy the same folder into that directory instead.

Use the repository's `install.py` for all installation work, dependencies, services, and hooks.
Keep this skill a guide; do not write service files or agent settings yourself.

## Select a release install and show defaults

Use a tagged release by default. Do not clone `main` for a normal install.
If cwd is inside Tether, ask: release install / “I want to develop Tether”.
Do not silently use that checkout. Keep every checkout and all user data.

1. Read `~/.config/tether/install.json` if present, or `TETHER_INSTALL_STATE` when set.
   If its `repo` is a git checkout, use the migration branch below before normal setup.
2. For an existing release install, reuse `~/.local/share/tether/current`.
   For a first install, run `python3 <this-skill-directory>/bootstrap.py` from any directory.
   Resolve the script beside this SKILL.md, not relative to cwd.
   The Codex copy is byte-identical to `integrations/claude/tether/skills/setup/bootstrap.py`.
3. Bootstrap downloads `tether-<version>.tar.gz` and `SHA256SUMS` from the latest stable GitHub release.
   It uses `TETHER_REPO` (default `incmak/tether`) and the CLI's latest-release endpoint.
   It verifies SHA-256, rejects unsafe paths and links, and atomically sets `current`.
   It writes only `~/.local/share/tether/releases/<version>/`, `current`, and the release lock.
   It does not install services or change agent settings.
   Stop on failure. Never fall back to an unverified archive or a git clone.
4. Bootstrap and `current/tether install` accept `--to vX.Y.Z` for a published tag.
   Offline installs require both `--archive PATH --sums PATH`; use the original `tether-<version>.tar.gz` filename.
   `tether install` prepares the same layout; it does not apply the installer.
5. Read `current/install.py` and run `python3 ~/.local/share/tether/current/install.py --help`.
6. Run `python3 ~/.local/share/tether/current/install.py --plan --json --no-build-ui` with no other overrides.
   The release archive contains `ui/dist`; do not require Node or rebuild it.
7. Show URL, host/auth, ports, features, updates, reused choices, checks, legacy units, hooks, and `cli_link`.
   Include failed checks and their `fix` text. Exit code zero alone does not prove readiness.
   Show plan warnings, including the shell line when `~/.local/bin` is absent from PATH.
   Apply creates `~/.local/bin/tether -> ~/.local/share/tether/current/tether`.
   It preserves any foreign command at that path and reports the conflict.

### Migrate an existing checkout install

Offer migration to a release, or the explicit developer choice. Do not replace saved choices with defaults.
Explain that migration moves services, update units, Claude hooks, and Tether status lines to `current`.
It keeps tokens, preferences, chat data, transcripts, and the checkout.
Local checkout changes produce a warning; migration does not modify the checkout.

Show the saved choices and ask for an explicit yes before migration.
After yes, run bootstrap if no release is prepared, then run:

```sh
python3 ~/.local/share/tether/current/tether migrate
```

Migration reads the saved `repo`; `--from PATH` must match it.
It accepts the same tag/archive flags as bootstrap. Pass the same flags when bootstrap used a specific archive or tag.
Migration reuses a prepared release only when its contents match the verified archive.
It applies `current/install.py` with the saved choices and `--no-build-ui`, then checks `/chat/`.
A failed installer, restart, or health check restores checkout units, hooks, status lines, and saved state.
Report failure clearly. Never delete the checkout; after success explain that the user can remove it.
Then use the release plan to review any further settings changes.

### Explicit developer choice

Only when the user chooses “I want to develop Tether”, ask where to use or clone the checkout.
Offer `~/tether`, preserve existing files, and reuse an existing Tether checkout when selected.
Otherwise clone `https://github.com/incmak/tether.git` there.
Use that checkout's `install.py --plan --json` and `--apply --json`.
Keep its default UI build choice; development needs Node and npm.
Use `git pull` and reapply for checkout updates. Release installs update with `tether update`.

## Ask once for choices

Codex CLI has no AskUserQuestion tool.
Ask these choices in one numbered message with at most four questions, then wait for the user's reply.
Use the baseline plan's feature choices as defaults and label them in the question.
Do not ask for ports, dependencies, or build choices unless the plan reports a failure that requires a decision.

1. **Features**, choose any combination: interactive terminal, voice-to-text, push notifications, usage hooks, host resources, small photos.
   Explain briefly: terminal needs ttyd; voice adds a local transcription worker; push needs browser support and HTTPS. Resources shows host usage in Settings. Small photos installs Pillow, so backdrop photos load fast on a slow network.
   Usage hooks add status lines only where the installer permits them.
   An explicit none disables all six features; an omitted answer retains the baseline choices.
2. **Remote access** (phone, tablet, other computers), single choice: Tailscale / same Wi-Fi LAN / HTTPS reverse proxy / this computer only.
   Recommend Tailscale for phone access; this computer only offers no remote phone access.
   Include the LAN plain-HTTP warning below in this choice.
3. **Updates**, single choice: notify / auto / off; recommend notify unless the user selects auto.
   Pass it as `--updates notify|auto|off`; `auto` adds a daily update timer.
   A git checkout updates with `git pull`; release installs use `tether update`.
4. **Legacy units**, only when `legacy_units` is nonempty: keep / replace; default keep.
   List the reported units and explain that replace disables and stops them.

## Map choices, review the diff, and apply

Use an explicit flag for each feature, including features the user disables:

| Choice | Selected | Not selected |
| --- | --- | --- |
| Interactive terminal | `--terminal` | `--no-terminal` |
| Voice-to-text | `--voice` | `--no-voice` |
| Host resources | `--resources` | `--no-resources` |
| Push notifications | `--push` | `--no-push` |
| Small photos | `--images` | `--no-images` |
| Usage hooks | `--hooks` | `--no-hooks` |

| Remote access | Installer flags |
| --- | --- |
| Tailscale | `--host 127.0.0.1 --auth token` |
| Same Wi-Fi LAN | `--host 0.0.0.0 --auth token` |
| HTTPS reverse proxy | `--host 127.0.0.1 --auth proxy` |
| This computer only | `--host 127.0.0.1 --auth token` |

1. Retain the baseline chat port through `--port` unless the user chooses another port to resolve a failed check.
   For releases, always pass `--no-build-ui`. For explicit development, retain the default build choice.
2. Add `--replace-legacy` only when the user selects replace.
3. Re-run `python3 ~/.local/share/tether/current/install.py --plan --json --no-build-ui` with the chosen flags.
   For explicit development, use the checkout installer instead.
4. Show a short before/after table for changed options, checks, units, commands, hooks, and legacy actions.
   State that apply can install dependencies, build the UI, save choices, and start or restart the listed services.
5. Resolve failed checks before apply; keep necessary follow-up questions limited to the reported failure.
6. Ask for an explicit yes to apply this reviewed plan, including any legacy replacement.
   Wait for the answer; a menu choice does not authorize apply.
7. After yes, run `python3 ~/.local/share/tether/current/install.py --apply --json --no-build-ui` with exactly the reviewed flags.
   For explicit development, use the checkout installer instead.
   Report a failed command as a failure; do not claim setup succeeded.
   If the returned plan differs materially from the review, report the difference.

## Print the result and phone steps

Print the applied plan's `url`, `token_file` location, selected features, and updates choice.
Never print the token itself.
Explain that proxy mode uses the proxy's sign-in rather than the Tether token.
Use the returned port in examples; do not treat a wildcard or loopback URL as a phone address.

- **Tailscale:** connect the computer and phone to the same tailnet.
  Show `tailscale serve http://127.0.0.1:<port>` as a command for the user to run.
  Open the HTTPS address that Serve prints, with `/chat/` appended, then sign in with the Tether token.
  Follow any HTTPS enablement prompt from Serve.
  See [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).
- **Same Wi-Fi LAN:** connect both devices to the same trusted Wi-Fi network.
  Open `http://<computer-LAN-IP>:<port>/chat/` and sign in with the Tether token.
  Warn: plain HTTP sends the token and chat without encryption; push and home-screen installation need HTTPS.
- **HTTPS reverse proxy:** use an HTTPS proxy that requires sign-in and forwards to the loopback chat port.
  Link to [the nginx auth proxy example](https://github.com/incmak/tether/blob/main/server/deploy/examples/nginx-auth-proxy.conf).
  Also identify the local file `server/deploy/examples/nginx-auth-proxy.conf`; replace its example port with the chosen port.
  Open the configured HTTPS `/chat/` URL on the phone and use the proxy's sign-in.
  Mark the public URL pending if the proxy is not yet configured.
- **This computer only:** open the returned loopback URL on this computer and sign in with the Tether token.
  State that the phone cannot reach this mode.

Start an agent inside tmux, then select its session in Tether.
For HTTPS phone access, offer Add to Home Screen and notification permission when push was selected.
Leave network setup as the printed next steps; installer success alone does not prove phone access.
