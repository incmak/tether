"""Write cases.json beside this script by default, or pass an output path."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from friday_agent import SendProposal, send_readback, tool_schemas  # noqa: E402
TOOLS = {t['function']['name'] for t in tool_schemas()}

STATE = {"busy": ["login-fix"], "idle": 5,
         "needs": [{"chat": "deploy-check", "needs": "Allow Bash command: npm test?"}],
         "target": "friday test"}
CHATS = {"target": "friday test", "chats": [
    "friday test, Claude, idle, in Tether", "login-fix, Claude, working, in Tether",
    "api-server, Codex, idle, in Backend", "docs-site, Claude, idle, in Docs",
    "deploy-check, Claude, needs you, in Backend", "Automation tether, Claude, idle, in Automation", "Automation tether . Can we have…, Claude, idle, in Automation",
    "release-notes, Claude, idle, in Docs"], "sessions": ["Tether 2", "Backend 2", "Docs 2", "Automation 1"]}
RESULTS = {
    "list_chats": CHATS,
    "backlog_list": {"count": 3, "items": ["B12 Add a health check endpoint", "B7 Fix broken links on the docs home page",
                                            "B30 Dark mode for settings (in progress)"]},
    "read_last_reply": {"chat": "api-server", "text": "I added the health check endpoint and the tests pass."},
}

def call(id_, name, args):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

def turn(user, name, args, result, reply, id_):
    return [{"role": "user", "content": user},
            {"role": "assistant", "content": None, "tool_calls": [call(id_, name, args)]},
            {"role": "tool", "tool_call_id": id_, "content": json.dumps(result)},
            {"role": "assistant", "content": reply}]

def say(user, reply):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]

cases = []
def case(id_, group, split, user, expect, source="hand", history=None, **extra):
    cases.append(dict(id=id_, group=group, split=split, source=source, history=history or [], user=user,
                      expect=expect, **extra))

CANT = "(?i)can(?:'t|not| ?not)|unable|don't have|no way|not able|isn't something"
NEEDS = say("Yeah, see which chat needs my attention. Let's start with the oldest.",
            "deploy-check needs your attention: it asks to allow the command npm test.")
# ---- From the 19 Nov 2026 log (names replaced) ----
case("log-ask-why-slow", "send_direct", "dev",
     "Okay, so I reckon you took too much uh But I'll can you ask the Frida test session, like why did Friday took too long to respond?",
     {"tool": "send_to_chat", "args": {"chat": "(?i)friday test"}}, "log",
     history=say("Hi. Hi Friday are you listening?", "I am listening. What can I help you with today?"))
case("log-catch-up-count", "backlog_list", "dev",
     "Okay, okay, that's great. And can you check like what's on my table, like what I have to catch up with? How many items?",
     {"tool": "backlog_list", "any_tool": ["backlog_list", "list_chats"]}, "log")
case("log-needs-attention", "status", "dev", "Yeah, see which chat needs my attention. Let's start with the oldest.",
     {"tool": None, "reply": "(?i)deploy"}, "log")
case("log-open-in-tether", "cant_do", "dev", "Okay, can you open that session in the teeth so that I can see it?",
     {"tool": None, "reply": CANT}, "log", history=NEEDS)
case("log-switch-in-window", "cant_do", "dev",
     "No, you switch that in this uh like uh this Friday session. But I want you to uh switch that within the Ther window.",
     {"tool": None, "reply": CANT}, "log",
     history=NEEDS + turn("Okay, can you open that session in the teeth so that I can see it?", "switch_chat",
                          {"chat": "deploy-check"}, {"ok": True, "chat": "deploy-check"},
                          "I have switched to the deploy-check chat.", "h1"))
case("log-go-to-session", "cant_do", "dev", "And can you go to that session?",
     {"tool": None, "reply": CANT}, "log", history=NEEDS)
case("log-you-havent", "no_tool", "dev", "No you haven't. I can I cannot see it. Like what are you talking about?",
     {"tool": None}, "log",
     history=NEEDS + say("And can you go to that session?", "I have opened the sidebar and switched to that chat for you."))
case("log-split-ask", "split_turn", "dev",
     ["Uh it was already open. Okay, let's ask uh Friday test uh chat session to go through these logs and spin up a new tab so that it can kind of do you understand like it can then",
      "Create and work on this log and then accordingly fine-tune this model so that it can understand intent."],
     {"tool": "send_to_chat", "args": {"chat": "(?i)friday test", "prompt": "(?i)log"}}, "log")
READBACK = "I will ask the friday test chat to go through the logs, spin up a new tab, and work on fine-tuning the model for intent. Shall I send that?"
case("log-confirm-plus-more", "confirm", "dev", ["Please do that.", "And also ask it to fix B three one eight."],
     {"tool": "send_to_chat", "args": {"chat": "(?i)friday test", "prompt": "(?i)318|three one eight"}}, "log",
     history=say("Okay, let's ask Friday test to go through the logs and fine-tune the model to understand intent.", READBACK))
case("log-praise", "no_tool", "dev", "Great, I love it. I love how you're uh responding well", {"tool": None}, "log")
case("log-urgent-feature-ask", "send_direct", "dev",
     "And kind of I was thinking if I am needed, um at times we should have a feature if I'm needed urgently, like very urgently, not urgent, like very urgently, and it's a critical blocker and no way out. And you can just call me through Friday and then I can answer it. Can we have that feature and can we ask the Friday test session to spin up a new tab? And like ask um to write a spec and use the skill grill me with dogs.",
     {"tool": "send_to_chat", "args": {"chat": "(?i)friday test", "prompt": "(?i)spec"}}, "log")
case("log-confirm-yes-please", "confirm", "dev", "Yes, please.",
     {"tool": "send_to_chat", "args": {"chat": "(?i)friday test"}}, "log",
     history=say("We should have a feature where Friday calls me when a critical blocker needs me. Friday test should write a spec for it.",
                 "I will ask the friday test chat to spin up a new tab and write a spec for an urgent voice call feature. Shall I send that?"))
case("log-queue-question", "no_tool", "dev",
     "Did you force it or it went naturally into the queue and then automatically this next. Session call picked it up.",
     {"tool": None, "claim_ok": True}, "log",
     history=turn("Yes, please.", "send_to_chat", {"chat": "friday test", "prompt": "Write a spec for an urgent voice call feature."},
                  {"ok": True, "chat": "friday test"}, "I have sent that to the friday test chat.", "h2"))
case("log-move-to-automation", "misheard", "dev", "Okay let's move to The automation teeth are session.",
     {"tool": "switch_chat", "args": {"chat": "(?i)automation"}}, "log")
case("log-latest-update", "read_reply", "dev", "What is the latest uh update from it?",
     {"tool": "read_last_reply", "args": {"chat": "(?i)automation"}}, "log",
     history=turn("Okay let's move to The automation teeth are session.", "switch_chat", {"chat": "Automation tether"},
                  {"ok": True, "chat": "Automation tether"}, "I have switched to the Automation tether chat.", "h3"),
     tool_results={"read_last_reply": {"chat": "Automation tether", "text": "The watcher restarted. Two choices wait for you."}})
case("log-event-done", "event", "dev",
     '{"chat":"friday test","kind":"done","text":"Friday waits for several slow steps before it speaks. I logged a fix as backlog B318."}',
     {"tool": None}, "log", event=True)

# ---- From the 19 Nov 2026 00:13-00:15 call (user evidence; names replaced) ----
AUTO = "Automation tether . Can we have…"
AUTO_CTX = turn("What is the latest uh update from it?", "read_last_reply", {"chat": AUTO},
                {"chat": AUTO, "text": "Two choices wait for you. Auto-merge: reply C to keep Observe, my recommendation. The Factory floor: build it, change it, or drop it."},
                "The latest update asks you to reply about auto-merge and decide on the Factory floor.", "h5") + \
    say("What is auto motion and what to reply?",
        "Auto-merge merges approved work on its own. The chat recommends replying C to keep observing.")
AUTO_STATE = dict(STATE, target=AUTO)
case("log-suggest-misheard", "readback", "dev", "Can you check full context and then search to me what we should do?",
     {"tool": None, "readback_chat": AUTO}, "log", history=AUTO_CTX, state=AUTO_STATE)
RB_AUTO = f"I will ask {AUTO} to review the full context and suggest what we should do. Shall I send that?"
case("log-yes-plus-more", "split_turn", "dev",
     ["Yes, please do.", "And for the factory floor, uh I like the design, but I'll still want it to grill me with dogs so that we can have better specs."],
     {"tool": None, "readback_chat": AUTO}, "log", state=AUTO_STATE,
     history=AUTO_CTX + say("Can you check full context and then search to me what we should do?", RB_AUTO))
BLOCKED_SEND = [{"role": "user", "content": "Yes, please do."},
                {"role": "assistant", "content": None, "tool_calls": [call("h6", "send_to_chat", {"chat": AUTO, "prompt": "Review the full context and recommend next steps."})]},
                {"role": "tool", "tool_call_id": "h6", "content": json.dumps({"error": "Read the prompt back and ask the user to confirm before sending."})},
                {"role": "assistant", "content": "I have sent that to the automation tether chat."}]
case("log-are-you-sure", "no_tool", "dev", "Are you sure? Because I don't see it.",
     {"tool": None, "reply": "(?i)\\bnot\\b|didn't|wasn't|haven't|hasn't|sorry"}, "log", state=AUTO_STATE,
     history=AUTO_CTX + BLOCKED_SEND)
case("log-not-approval", "no_tool", "dev",
     ["Okay, okay, yeah.", "Okay, please like I was asking are you sure because I don't see it."],
     {"tool": None}, "log", state=dict(AUTO_STATE, needs=[]),
     history=AUTO_CTX + BLOCKED_SEND + say("Are you sure? Because I don't see it.",
                                           "I am sorry, there was a technical block. Shall I send that prompt to the automation tether chat now?"))
case("log-unknown-session", "misheard", "dev",
     "Okay, okay. Now hold on, I'll see what's happening in The teeth. Sorry in the predicta session.",
     {"tool": None, "reply": "(?i)which|no session|couldn't find|could not find|can't find|don't see|not find|didn't find"}, "log",
     tool_results={"list_chats": {"error": "No session called predicta. Sessions: Tether, Backend, Docs, Automation, Friday chats"}})

# ---- From the 19 Nov 2026 13:51-13:52 call (user evidence) ----
AUTO_CHAT = r"(?i)^Automation tether \. Can we have…$"
case("log-answer-only-c", "send_direct", "dev", "tell it let's keep option C",
     {"tool": "send_to_chat", "args": {"chat": AUTO_CHAT, "prompt": r"(?i)\bC\b"},
      "prompt_not": "(?i)yes to all"}, "log", state=AUTO_STATE,
     history=turn("Uh so like what's happening in automation Ther? Like can you check it? Is there a dependency on me?",
                  "read_last_reply", {"chat": AUTO}, {"chat": AUTO, "text":
                      'B240 and B241 are still in progress in B232 Extension Zip. I am watching that tab.\n\n'
                      'You still have 2 open decisions:\nAuto-merge: "C" keeps Observe mode.\n'
                      'Factory floor: grill questions Q1 to Q5.\n\n'
                      'Next: reply "C, yes to all", or give the numbers you want changed.\n\nNeeds you: yes'},
                  "B240 and B241 are still in progress, and it is waiting for your answers on auto-merge and factory floor questions.", "h7")
     + say("So what is the auto-most question?", "It asks whether to keep Observe mode with option C.")
     + say("Do you think we should keep that?", "I cannot judge your design choices, but option C keeps Observe mode active."))
case("log-answer-only-q7", "send_direct", "dev", "tell it yes to Q7 only",
     {"tool": "send_to_chat", "args": {"chat": AUTO_CHAT, "prompt": r"(?i)\bQ?7\b"},
      "prompt_not": "(?i)yes to all"}, "log", state=AUTO_STATE,
     history=turn("What did it say about the Factory floor?", "read_last_reply", {"chat": AUTO},
                  {"chat": AUTO, "text": 'Factory floor, round 2: questions Q6 to Q9.\n\n'
                                        'Next: reply "yes to all", or give the numbers you want changed.'},
                  "The chat is waiting for your answers to questions Q6 to Q9.", "h8"))

# ---- Spec b34 (reminder tools). Added only once reminder_add exists. ----
if 'reminder_add' in TOOLS:
    case("reminder-not-backlog", "reminder", "dev", "keep those all in reminders",
         {"tool": "reminder_add"},
         history=say("remind me to call the bank tomorrow and to renew the domain",
                     "Do you want those as reminders or as backlog items?"))

# ---- Negated send words (review finding) ----
case("neg-dont-send", "no_tool", "dev", "don't send that to api-server yet, I want to think",
     {"tool": None}, history=say("rate limiting on uploads would help", "Shall I send add rate limiting to the upload endpoint to api-server?"))
case("h-neg-not-tell", "no_tool", "holdout", "no, do not tell login-fix about that",
     {"tool": None}, history=say("the reset page also breaks", "Shall I send fix the password reset page to login-fix?"))

# ---- Hand-written: dev ----
D, H = "dev", "holdout"
case("send-tell", "send_direct", D, "tell api server to add a health check endpoint",
     {"tool": "send_to_chat", "args": {"chat": "(?i)api.server", "prompt": "(?i)health"}})
case("send-ask-misheard", "send_direct", D, "ask the docs sight chat to fix the broken links on the home page",
     {"tool": "send_to_chat", "args": {"chat": "(?i)docs.site", "prompt": "(?i)link"}})
case("send-have-it", "send_direct", D, "have login-fix run the tests again",
     {"tool": "send_to_chat", "args": {"chat": "(?i)login.fix", "prompt": "(?i)test"}})
case("readback-should", "readback", D, "the api server should add rate limiting to the upload endpoint",
     {"tool": None, "readback_chat": "api-server"})
case("readback-let", "readback", D, "let docs site update the changelog for the new release",
     {"tool": None, "readback_chat": "docs-site"})
RB_API = "I will tell api-server to add rate limiting to the upload endpoint. Shall I send that?"
case("confirm-yes", "confirm", D, "yes",
     {"tool": "send_to_chat", "args": {"chat": "(?i)api.server", "prompt": "(?i)rate"}},
     history=say("the api server should add rate limiting to the upload endpoint", RB_API))
case("confirm-go-ahead", "confirm", D, "yeah go ahead",
     {"tool": "send_to_chat", "args": {"chat": "(?i)api.server"}},
     history=say("the api server should add rate limiting to the upload endpoint", RB_API))
case("deny-no-wait", "deny", D, "no, wait, not yet", {"tool": None},
     history=say("the api server should add rate limiting to the upload endpoint", RB_API))
PENDING_API = {"name": "send_to_chat", "chat": "api-server", "payload": "Add rate limiting to the upload endpoint"}
FIXED_API = say("the api server should add rate limiting to the upload endpoint",
                send_readback(SendProposal(**PENDING_API, turn=0)))
case("fixed-yes", "confirm", D, "yes please",
     {"tool": "send_to_chat", "args": {"chat": "api-server"}}, history=FIXED_API, pending=PENDING_API)
case("fixed-no", "deny", D, "no, not now", {"tool": None}, history=FIXED_API, pending=PENDING_API)
case("fixed-yes-plus-more", "split_turn", D, ["Yes, please do.", "And also check the logs after."],
     {"tool": "send_to_chat", "args": {"chat": "api-server"}}, history=FIXED_API, pending=PENDING_API)
case("approve", "approval", D, "approve it",
     {"tool": "answer_approval", "args": {"chat": "(?i)deploy", "decision": "approve"}})
case("deny-approval", "approval", D, "deny that request",
     {"tool": "answer_approval", "args": {"chat": "(?i)deploy", "decision": "deny"}})
case("approval-what", "status", D, "what is deploy check asking for?", {"tool": None, "reply": "(?i)npm|test"})
case("read-reply", "read_reply", D, "what did the api server say?",
     {"tool": "read_last_reply", "args": {"chat": "(?i)api.server"}})
case("busy-finished", "status", D, "did login fix finish yet?",
     {"tool": None, "reply": "(?i)work|busy|running|not yet|still|not finished"})
case("list-idle", "list_chats", D, "which chats are idle right now?", {"tool": "list_chats"})
case("list-session-email", "misheard", D, "what chats are in the docs email session?",
     {"tool": "list_chats", "args": {"session": "(?i)docs"}})
case("switch", "switch", D, "switch to docs site", {"tool": "switch_chat", "args": {"chat": "(?i)docs.site"}})
case("panel-terminal", "panel", D, "open the terminal", {"tool": "open_panel", "args": {"panel": "terminal", "open": "True"}})
case("panel-close-preview", "panel", D, "close the preview", {"tool": "open_panel", "args": {"panel": "preview", "open": "False"}})
case("new-chat", "new_chat", D, "start a new codex chat called db-migrate in the backend project",
     {"tool": "new_chat", "args": {"agent": "codex", "name": "(?i)db.migrate", "project": "(?i)backend"}})
case("backlog-list", "backlog_list", D, "what's on my backlog?", {"tool": "backlog_list"})
case("backlog-add", "backlog_add", D, "add to the backlog: dark mode for the login page",
     {"tool": "backlog_add", "args": {"text": "(?i)dark mode"}})
case("backlog-remember", "reminder", D, "remember to rotate the staging API keys next week",
     {"tool": "reminder_add", "args": {"reminders": "(?i)rotate"}})
case("backlog-send", "backlog_send", D, "send B 12 to api server",
     {"tool": "backlog_send", "args": {"item": "(?i)b ?12", "chat": "(?i)api.server"}})
case("backlog-readback", "readback", D, "B 12 for api server I think",
     {"tool": None, "readback_chat": "api-server", "readback_tool": "backlog_send"})
case("outside-github", "outside", D, "create a GitHub issue for the login bug", {"tool": None, "reply": CANT})
case("misheard-tell", "misheard", D, "tell friday chest to check the error logs",
     {"tool": "send_to_chat", "args": {"chat": "(?i)friday test"}})
case("thanks", "no_tool", D, "thanks Friday, that's all for now", {"tool": None})
case("what-can-you-do", "no_tool", D, "what can you do for me?", {"tool": None})
case("did-you-send-blocked", "no_tool", D, "did you send it?", {"tool": None, "reply": "(?i)\\bno\\b|not|didn't|haven't"},
     history=[{"role": "user", "content": "the api server should add rate limiting"},
              {"role": "assistant", "content": None, "tool_calls": [call("h4", "send_to_chat", {"chat": "api-server", "prompt": "Add rate limiting."})]},
              {"role": "tool", "tool_call_id": "h4", "content": json.dumps({"error": "Read the prompt back and ask the user to confirm before sending."})},
              {"role": "assistant", "content": "I will tell api-server to add rate limiting. Shall I send that?"}])
case("event-needs", "event", D, '{"chat":"deploy-check","kind":"needs","text":"Allow Bash command: npm test?"}',
     {"tool": None}, event=True)

# ---- Hand-written: holdout (do not tune on these) ----
case("h-send-message", "send_direct", H, "message the api server and say rebase on main and push",
     {"tool": "send_to_chat", "args": {"chat": "(?i)api.server", "prompt": "(?i)rebase"}})
case("h-send-get-it", "send_direct", H, "can you get login fix to add a test for the password reset",
     {"tool": "send_to_chat", "args": {"chat": "(?i)login.fix", "prompt": "(?i)password"}})
case("h-readback-want", "readback", H, "I want login fix to also cover the password reset page",
     {"tool": None, "readback_chat": "login-fix"})
case("h-readback-idea", "readback", H, "docs site needs a page about the new settings screen",
     {"tool": None, "readback_chat": "docs-site"})
RB_DOCS = "I will ask docs-site to add a page about the new settings screen. Shall I send that?"
case("h-confirm-sure", "confirm", H, "sure, do it", {"tool": "send_to_chat", "args": {"chat": "(?i)docs.site"}},
     history=say("docs site needs a page about the new settings screen", RB_DOCS))
PENDING_DOCS = {"name": "send_to_chat", "chat": "docs-site", "payload": "Update the changelog for the new release"}
case("h-fixed-yes", "confirm", H, "yeah go ahead",
     {"tool": "send_to_chat", "args": {"chat": "docs-site"}}, pending=PENDING_DOCS,
     history=say("docs site should update the changelog for the new release",
                 send_readback(SendProposal(**PENDING_DOCS, turn=0))))
case("h-deny-dont", "deny", H, "actually no, don't", {"tool": None},
     history=say("docs site needs a page about the new settings screen", RB_DOCS))
case("h-approve-allow", "approval", H, "yes allow it",
     {"tool": "answer_approval", "args": {"decision": "approve"}})
case("h-deny-reject", "approval", H, "no, reject the deploy check one",
     {"tool": "answer_approval", "args": {"decision": "deny"}})
case("h-read-misheard", "misheard", H, "what did dogs site reply?",
     {"tool": "read_last_reply", "args": {"chat": "(?i)docs.site"}},
     tool_results={"read_last_reply": {"chat": "docs-site", "text": "The links are fixed."}})
case("h-list-tmux", "list_chats", H, "what chats are in the tether tea mux session?",
     {"tool": "list_chats", "args": {"session": "(?i)tether"}})
case("h-switch-default", "switch", H, "make api server the default chat", {"tool": "switch_chat", "args": {"chat": "(?i)api.server"}})
case("h-panel-changes", "panel", H, "show me the changes", {"tool": "open_panel", "args": {"panel": "changes"}})
case("h-new-chat", "new_chat", H, "open a new claude chat named release check",
     {"tool": "new_chat", "args": {"agent": "claude", "name": "(?i)release"}})
case("h-backlog-read", "backlog_list", H, "read me the top items on my backlog", {"tool": "backlog_list"})
case("h-backlog-add", "backlog_add", H, "put fix the flaky upload test on my backlog",
     {"tool": "backlog_add", "args": {"text": "(?i)flaky"}})
case("h-backlog-send", "backlog_send", H, "send backlog item B seven to docs site",
     {"tool": "backlog_send", "args": {"item": "(?i)b ?7|seven", "chat": "(?i)docs.site"}})
case("h-outside-email", "outside", H, "send an email to the team with the release notes", {"tool": None, "reply": CANT})
case("h-cant-show", "cant_do", H, "bring up the api server chat on my screen", {"tool": None, "reply": CANT})
case("h-misheard-theatre", "misheard", H, "ask the automation theatre chat to restart the watcher",
     {"tool": "send_to_chat", "args": {"chat": "(?i)automation"}})
case("h-no-tool-joke", "no_tool", H, "ha, that was quick", {"tool": None})
case("h-split-tell", "split_turn", H, ["okay tell api server to", "add logging to the upload handler"],
     {"tool": "send_to_chat", "args": {"chat": "(?i)api.server", "prompt": "(?i)log"}})
case("h-status-needs", "status", H, "does anything need me right now?", {"tool": None, "reply": "(?i)deploy"})

output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("cases.json")
with output.open("w", encoding="utf-8") as stream:
    json.dump({"version": 1, "state": STATE, "tool_results": RESULTS, "cases": cases}, stream, indent=1)
print(len(cases), sum(c["split"] == "dev" for c in cases))
