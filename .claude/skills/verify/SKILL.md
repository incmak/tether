---
name: verify
description: Screenshot proof for a Tether UI change. Use before a commit that touches ui/ or when asked to show a surface at 320 px and 1280 px. Runs the real UI on fixtures.
---

# Verify a UI change

1. Name each changed surface. Run `node ui/tools/verify.cjs --list` for the built-in surfaces. Add a recipe for any other surface.
2. From the repository root, run `node ui/tools/verify.cjs --surface settings,backlog`. Add `--recipe FILE` to include custom surfaces. Add `--local` to use your own headless Chromium. Both browser modes use the same routing and event stream stub. The run builds the current sources, uncommitted changes included, into a temporary folder; `ui/dist` stays as it is. If `ui/node_modules` is missing, run `cd ui && npm ci` first (add `--onnxruntime-node-install-cuda=skip` if the CUDA download stops it).
3. Fix each FAIL and run again. A 403 names the route. Check that the route cannot reach the host before you add it to `ALLOWED` in `server/dev/verify_server.py`. You can also stub it with `page.route` in the recipe.
4. Look at every screenshot. Check spacing, wrapping, contrast, and the intended state. The run saves shots and then `manifest.json` in `.release-work/<day>/verify/<label>-<HHMMSS>`. Use `--name` to set the label or `--shots NEW_DIR` to choose a new destination.
5. Copy the printed `screenshots(...)` item into the commit's `Verified:` line. The driver prints it only when it builds the UI, the source ID matches before and after the run, all shots pass, and the widths include 320 px and 1280 px. The manifest records the IDs. A run can pass without a stamp if the sources change, it uses `--dist`, or it omits a required width.

## Recipes

A `.cjs` recipe exports surfaces by name. Keep a one-use recipe outside the repository.

```js
module.exports = {
  'backlog-error': {
    open: async (page, ctx) => {
      await page.route('**/mobile/api/projects/backlog', route => route.fulfill({ status: 500, json: { error: 'Fixture error' } }))
      if (ctx.phone) await page.getByRole('button', { name: 'Open sessions' }).click()
      await page.getByRole('button', { name: /^Backlog/ }).first().click()
    },
    landmark: 'dialog.backlog-sheet[open] >> text=Fixture error',
    expect: { 'projects/backlog': 500 },
  },
}
```

- Scope the landmark to the open surface. At capture, the driver checks that it fits inside the viewport and that two points on it are exposed. An optional `ready(page, ctx)` waits for the state you need. See the built-in surfaces for phone menu controls.
- Use `expect` for an intended API status. Use the API path after `/mobile/api/`, without a query string. The shot fails if the expected status never arrives. A recipe's `page.route` can supply the response.
- The fixture has one idle Claude conversation and four backlog items. Edit `seed()` in `server/dev/verify_server.py` to add fixture data.

## Limits

- The route and program allowlists and the tmux wrapper keep the server on fixture data. For discovery, `ps -axo` reads PIDs, parent PIDs and process names (`ucomm` on macOS, `comm` on Linux). It reads no arguments or environment. Discovery keeps only the fixture pane and its descendants. The wrapper uses a private socket and permits two named metadata writes. The launcher rejects builds with symlinks.
- The audit hook is a safety net for Tether's own code. It provides no sandbox for hostile code in the server process. [Python audit hook documentation](https://docs.python.org/3.12/library/sys.html#sys.addaudithook)
- Programs that an allowed program starts run outside the Python hook. The allowlists restrict the allowed programs to operations that start no other host program.
- The fixture pane runs `sleep`. Check screen-driven writes on a throwaway tab as described in `AGENTS.md`. Run `node ui/tools/verify.cjs --help` for other options.
