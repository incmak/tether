# Responsive sweep: Files lane

Audience: Tether maintainers. Question: do Files, Share and their viewers remain usable at phone and desktop widths? Scope: the files lane from base `833a745`, with fixture-only browser writes.

Branch: `resp-files`. The shared specification is copied to `../SPEC.md`. No push, merge or deployment is part of this lane.

## Verification

Final result: PASS — 289 checks at all seven widths. The same final harness fails on base `833a745` at every width. `checks.json` records the results. `typecheck.log` records the TypeScript pass; `lint.log` reports 0 errors and 94 existing warnings; `build.log` records a successful Vite build. `existing-phone-bar.log` ends `phone bar: PASS`.

Required viewports: 320×640, 390×844, 740×360, 768×1024, 1024×768 and 1440×900. Extra viewport: 900×640, the desktop threshold. Phone contexts use a coarse pointer.

Fixtures include two different unbroken names (80 W characters and a 100-character folder name), an 80-character chat name, 130 files selected, and 30 uploads with a failure and a long error. Word and slides fixtures are in `fixtures/`. The workbook and PDF are generated in the test.

The same harness runs against an untouched archive of base `833a745` and the lane build. Run one viewport per process to stay within the execution host's job limit:

```sh
cd ui
WIDTH=320 PHASE=after node test_responsive_files_ui.cjs
WIDTH=320 PHASE=before DIST=../.release-work/2026-11-16/responsive/files/baseline/ui/dist node test_responsive_files_ui.cjs
```

Repeat for 390, 740, 768, 900, 1024 and 1440. The default command runs those widths in child processes, with a 120-second deadline for each child. Each child closes its browser and HTTP server.

The tests check page and local scroll widths, control text, viewport bounds, control overlap and 24px targets. They exempt inline prose links under WCAG 2.5.8, native checkbox spacing, tile selection overlays, and the existing floating Share selection bar/FAB over scrollable content. Target checks still run inside scroll containers. Full accessible names remain on ellipsis labels. This is a scoped responsive check, not a claim of full product WCAG conformance.

## Issues and evidence

Each row is a Verified decision. The full-size critical captures and all 28 before/after overview sheets were inspected. The overviews are in `shots/overviews/`. Screenshot paths are relative to this directory. All captures remain in `shots/before/` and `shots/after/`.

| Screen | Viewport | What broke and fix | Source | Before / after screenshot |
|---|---|---|---|---|
| Phone Files list and picker rows | 320, 390, 740, 768 | The grid's implicit track kept long names at their content minimum. Use `minmax(0,1fr)` so names show an ellipsis. | `ui/src/ExplorerPhone.css` `.xp-row-text` | `shots/{before,after}/320-files-list.png` |
| Copy/move picker | All | Long titles, folder rows, breadcrumbs and destination buttons caused horizontal scroll or left the surface. Use bounded ellipsis spans, a fully wrapped current destination, and normal dialog button targets. Phone destination buttons wrap. | `Explorer.css`, `ExplorerWrites.tsx`, `ExplorerPhone.css` | `shots/{before,after}/1024-copy-long-folder.png`; `320-copy-second-folder.png` |
| Desktop Files selection | 900, 1024, 1440 | The action row lost controls to the right. Unrestricted wrapping also moved rows between clicks. Group changing actions and fixed controls. Only the long Add label shrinks. The container width sets the line break. | `Explorer.tsx`, `Explorer.css` `.fx-tb-actions` | `shots/{before,after}/1024-files-selection.png`; `900-files-130-selected.png` |
| File action menus and sheet path | All | A long chat name overflowed menu actions. A flex text node did not render its path ellipsis. Give dynamic menu labels a shrinking span; use a block for the plain path. | `ExplorerWrites.tsx`, `ExplorerPhone.tsx`, `Explorer.css`, `ExplorerPhone.css` | `shots/{before,after}/320-row-menu.png`; `1440-row-menu.png` |
| Upload failure panel and summary | All | Long failure text scrolled sideways. The phone summary used half the available width and could leave the viewport. Wrap messages, bound the panel/summary height, and scroll the desktop list under a fixed heading. | `Explorer.css` `.fx-uploads`; `ExplorerPhone.css` `.xp-toast` | `shots/{before,after}/1024-files-upload-failures.png`; `320-files-upload-failure.png` |
| Error states | All | Long server messages caused local horizontal scroll. Wrap unbroken text in the Files and Share error hosts. | `Explorer.css` `.fx-empty`; `ExplorerPhone.css` `.xp-empty`; `Share.css` `.sh-empty` | `shots/{before,after}/320-files-error.png`; `320-share-error.png` |
| Share header, preview actions and selection | 320, 390, 900 | Actions could leave the viewport or crush their labels. Let complete controls wrap. | `Share.css` `.sh-head`, `.sh-pane-actions`, `.sh-actionbar` | `shots/{before,after}/320-share-preview.png`; `900-share-selection.png` |
| Folder tab close and Share Retry | Desktop / all | Close targets were 22px; Retry was below 24px. Use 24px close targets, and 24px/44px Retry height for fine/coarse pointers. Scope the shared Retry fix to Share in this lane. | `Explorer.css` `.fx-tab-close`; `Share.css` `.retry-button` | `shots/{before,after}/1024-folder-tabs.png`; `320-share-error.png` |
| Word viewer | 320, 390, 740, 768 | The renderer centered an auto-width flex page. Its long words kept the page wider than the screen. Stretch the narrow page so the renderer's existing word wrap works. | `TextEditor.css` `.office-doc.narrow .docx-wrapper`; `OfficeViewer.tsx` stylesheet import | `shots/{before,after}/320-docx-viewer.png` |

## Coverage requirements

| Requirement | Status / evidence |
|---|---|
| Base and copied spec | Verified: `git log` starts at `833a745`; `../SPEC.md` matches the supplied spec. |
| Desktop Places, list/grid, long names and folder tabs | Verified: all seven `after-*.log` files pass; `*-files-list`, `*-files-grid`, `*-folder-tabs`. |
| Selection, 130 counts, stable rows, complete short labels | Verified: all seven `after-*.log` files pass; explicit row-top/label/count checks. |
| Copy/move picker, two long destination names | Verified: all seven `after-*.log` files pass; `*-copy-picker`, `*-copy-long-folder`, `*-copy-second-folder`, `*-move-picker`. |
| Rename, phone view/add/new-folder sheets, file menu | Verified: all seven `after-*.log` files pass; `*-rename`, `*-view-options`, `*-add-sheet`, `*-new-folder`, `*-row-menu`. |
| 30 uploads, failure, long error and scroll/close control | Verified: all seven `after-*.log` files pass; upload/failure/scroll checks. |
| Text preview, rich editor, source editor, link form | Verified: all seven `after-*.log` files pass; `*-text-*`, `*-rich-*`, `*-source-editor`. |
| Workbook tabs, Word, two-slide presentation, PDF | Verified: all seven `after-*.log` files pass; `*-office-*`, `*-docx-viewer`, `*-pptx-viewer`, `*-pdf-viewer`. |
| Share list, preview/rename, selection and 30 uploads | Verified: all seven `after-*.log` files pass; `*-share-*`. |
| Loading, empty and error states | Verified: all seven logs include Files and Share loading, empty and error states. |
| 180px resizable panels | Not applicable to these screens: `panelEdges.ts` registers sessions, inbox, status and waiting. Explorer's Places/Details are fixed-width screen sections. No synthetic 180px Explorer layout was invented. |
| Mandatory UI gates and existing coverage | Verified: `typecheck.log`, `lint.log`, `build.log`, `existing-phone-bar.log`. |
| Independent review | Verified: `REVIEW.md` records final APPROVED; clean-index phone run passes. |
| Scoped commit, no push/merge/deploy | Verified: this lane commit contains only owned source, the test, its fixtures and text evidence. No push, merge or deployment. |

## Files outside this lane

No source file outside the owned lane was edited. B172 records the shared follow-up:

- `ui/src/Retry.css`: give shared Retry controls a minimum 24px height and the established 44px coarse-pointer height. Share has the scoped fix here. Check all Retry hosts before the shared change.
- `ui/src/App.css`: `.sheet-sub` uses flex with raw text. Plain paths need block ellipsis; icon rows need a separate shrinking text span. The phone Files plain-path host has the scoped fix here.
- The existing shared Word rules live in `App.css`: the eventual shared change is to set `align-items: stretch` on `.office-doc.narrow .docx-wrapper`, then remove the corresponding `OfficeViewer.tsx` import of `TextEditor.css`. This lane adds the narrow alignment override in the owned `TextEditor.css`, with an explicit import from `OfficeViewer.tsx`.

## Processes and review

All seven final fixture runs completed. All fixture browsers and HTTP servers closed. Earlier combined commands received SIGTERM; separate per-width runs supplied the final evidence. One landscape browser close also required a fresh passing run. The clean-index fixture check runs from staged files with no untracked fixture dependency; its result is in `clean-checkout.log`. The fixture subagent completed its files and started no background process. The independent reviewer approved the staged change; its outcome is saved in `REVIEW.md`. The reviewer tab closes before handoff. No production process or backend restart is part of this task.

## Source locations

- `ui/src/ExplorerPhone.css:64` — `.xp-row-text`.
- `ui/src/Explorer.css:57` — `.fx-toolbar {`.
- `ui/src/Explorer.tsx:532` — `<div className="fx-tb-actions">`.
- `ui/src/Explorer.css:131` — `.fx-picker .sheet-heading`.
- `ui/src/ExplorerWrites.tsx:25` — `title={item.label}`.
- `ui/src/ExplorerPhone.tsx:345` — `className="xp-action-label"`.
- `ui/src/ExplorerPhone.css:94` — `.xp-sheet .sheet-sub`.
- `ui/src/Explorer.css:149` — `.fx-uploads {`.
- `ui/src/ExplorerPhone.css:91` — `.xp-toast.app-toast`.
- `ui/src/Explorer.css:173` — `.fx-empty {`.
- `ui/src/Share.css:23` — `.sh-empty {`.
- `ui/src/Share.css:6` — `.sh-head {`.
- `ui/src/Share.css:117` — `.sh-pane-actions {`.
- `ui/src/Share.css:96` — `.sh-actionbar {`.
- `ui/src/Explorer.css:39` — `.fx-tab .fx-tab-close`.
- `ui/src/Share.css:144` — `.share .retry-button`.
- `ui/src/TextEditor.css:88` — `.office-doc.narrow .docx-wrapper`.
- `ui/src/OfficeViewer.tsx:8` — `import './TextEditor.css'`.
