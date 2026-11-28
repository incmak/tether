# Codex 0.170.0 queued follow-up inputs

These frames came from a throwaway Codex 0.170.0 TUI tab on Linux.
Each frame uses `tmux capture-pane -e -p`. The pane is 100 columns by
40 rows except where noted.

- `queued_four.ansi` shows the queue during a tool call. It has a short
  prompt, a long prompt cut after three rows, a shell command, and a prompt
  with a line break. The edit hint follows the queue.
- `queued_four_60col.ansi` shows the same queue at 60 columns. The long
  prompt wraps earlier.
- `queued_blank_url.ansi` adds a prompt with an empty line and a URL
  that reaches the edge of the pane without an ellipsis.
- `queued_with_draft.ansi` shows the six queued prompts and a separate
  draft. The composer footer shows `tab to queue message`.
- `queued_many_40rows.ansi` shows all 18 entries and the edit hint.
- `queued_many_20rows.ansi` shows the same queue in a pane with 20 rows.
  The queue ends partway through an entry and has no edit hint.
- `after_esc.ansi` shows the interrupted turn. Codex restores queued
  prompts to the composer. The draft fills the composer and hides the footer.
- `idle_reply_quotes_queue.ansi` shows an idle tab with an assistant
  reply that quotes queue text.

There are no live steer-section captures for this version. The steer sent
during a tool call entered the transcript at once. The test for steer sections
uses the layout from the Codex widget source.
