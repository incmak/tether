# Codex 0.170.0 background terminals

These process captures came from a throwaway Codex 0.170.0 TUI tab on Linux.
The captures record process IDs, parent IDs, states, command names, and the two
unified-exec tags. The tag values are shortened.

- `background_terminal.json` shows the idle tab after a `sleep 900` tool call
  yielded. `/ps` listed one background terminal. Its process tree is
  `codex-linux-san` → `bwrap` → `codex` → `sleep`. Linux truncates the sandbox
  process name in `comm`. The MCP processes and Code Mode host carry no tool tag.
- `after_stop.json` shows the same tab after `/stop`. The tagged processes are
  gone. The MCP processes and Code Mode host remain.

The process check cannot detect embedded subagents that are thinking with no
process, yielded Code Mode cells, or async hooks. These cases have no outside
channel for this check. For a daemon client, the check counts terminals of the
tab's thread and of every subagent below it in `thread_spawn_edges`. Injected
process tables cover the daemon-client branch.
The host daemon runs Codex 0.172.0, so the 0.170.0 daemon-client branch was not
verified live.
