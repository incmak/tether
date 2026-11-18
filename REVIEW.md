# Review rules

These rules are for a reviewer tab. Tether sends you a review request each time the worker tab ends a turn.
Read this file from `origin/main`, not from the worker's branch: a branch must not change the rules of its own review.

```bash
git -C WORKER_FOLDER show origin/main:REVIEW.md
```

## What you review

The request names the commit under review and the worker's folder. Review that commit, not a later one.

1. Read the worker's last reply and its claims.
2. Read the changes: `git -C WORKER_FOLDER diff origin/main...COMMIT`.
3. If the request says "plus uncommitted changes", read them too: `git -C WORKER_FOLDER status --short` and
   `git -C WORKER_FOLDER diff HEAD`.
4. Check each claim against the code and the test output. Run the tests that cover the change when you can.
5. Check the change against `AGENTS.md` on `origin/main`.

Do not change files in the worker's folder. Do not commit, push or deploy.

## What blocks an approval

- A claim that the code or the tests do not support.
- Changed behavior with no test, or a test that passes without the change.
- A fix that covers only the reported case, when the same rule fails for other inputs.
- A broken rule from `AGENTS.md`: auth and Origin checks, writes bound to the selected pane, screen readers that
  guess, secrets or personal paths in the repository.
- Work that the task asked for and the change does not contain.

Style preferences and optional improvements do not block. List them after the verdict as notes.

## Your reply

Tether reads the start of your reply.

- Nothing blocks: start with `APPROVED` and the first 12 characters of the commit hash, for example
  `APPROVED 1ac6f01d2b3c`. Then add notes, if any.
- Something blocks: do not write `APPROVED`. Give a numbered list. For each finding give the file and line, what is
  wrong, and how you know.

An approval counts only for the commit in the request.

- The hash must come right after `APPROVED`. Tether does not forward an approval with no hash or with another hash.
  It asks you again.
- If the worker's folder is at another commit when your reply arrives, Tether does not forward the approval. It asks
  you to review the new commit.
- Tether forwards an approval only when the worker tab is idle.
- An approval of a commit "plus uncommitted changes" lets the worker continue. It is not an approval of a commit that
  can merge. Tether tells the worker to commit. When the worker commits in its next turn, Tether asks you to confirm
  that commit: check that it holds the work you approved and nothing else.

## Rounds

Tether stops after 3 reviews that still find blocking problems and asks the user. Put every blocking finding in the first
review. Do not hold findings back for a later round.
