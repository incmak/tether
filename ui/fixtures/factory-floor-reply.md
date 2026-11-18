You need to make 2 decisions. Auto-merge: reply "C". The Factory floor: answer the 5 questions below. Each "yes" accepts my recommendation.

`B122 Break-out Cleanup` still runs its full test suite. After that it does 1 more review round. You do not need to do anything for it.

## Auto-merge: keep Observe (option C)

- **What it is now:** Observe records a "would merge" decision. It merges nothing.
- **Why not A or B:** Enforce is only safe with a merge identity that the agents cannot reach. B230 found that the agents can run any command as root with no password (`sudo NOPASSWD: ALL`). They also hold your owner GitHub token, and that token can delete branch rules.
  - Option A gives the agents a new OS user without root. That takes several days and moves tmux, `~/.claude` and `~/.codex`.
  - Option B moves the merge authority to GitHub. That is a redesign.
- **The limit that stays:** with A or B, an agent can still fake a reviewer approval. You must accept that limit before Enforce is safe.
- **Payoff:** small. Each merge you approve by hand costs you about 1 tap. Auto-merge saves only those taps.
- **When to revisit:** after the Observe log has a few weeks of "would merge" decisions, compare them with what you merged. I did not count the decisions in the log yet.

## The Factory floor: grill round 1

I checked the prototype against these 3 documents:
- spec v3 (`spec-v3.md`)
- the prototype brief (`brief.md`)
- the prototype README (`README.md`)

Spec v3 says: "Each level is a list, not a map." The floor is a map. The floor also shows sample stages and states that are not real data. The questions below settle those 2 gaps first.

❓ **Q1 - Build order**: Should the list version of the Command center ship first? That covers spec v3: the Queue, the Goals list, the Task level and the command input. The floor comes after it as the 4th level, on the same data. Your 4 spec v3 questions decide the list version, so they come first:
1. May Tether approve a permission prompt under a rule you write?
2. Does Live mode go away?
3. How long does Tether keep your command history?
4. Does a guessed target need a yes, or a 5-second undo?

➡️ Yes. Ship the list version first, then the floor. A map shows wrong or missing data plainly. The list version makes the data correct first.

---

❓ **Q2 - What the floor is for**: Should the floor be an overview you choose to open? It sits as the 4th level above Goals, the way the night-shift prototype shows it. The Queue stays the default level, and the floor never replaces it. Other choices:
- (b) The floor replaces the Goals level.
- (c) The floor is only the big-screen wall view.

➡️ Yes, an overview you choose to open. Spec v3 keeps the Queue as the default because it answers "what needs me now". The floor answers a different question: "is the factory healthy and moving?"

---

❓ **Q3 - The phone**: On a phone, should the floor level show a simple list of bays? Each row shows the bay, how many tasks sit at each stage, and the beacons as rows. The full floor picture would be desktop and wall only. Spec v3 requires every level to work at 320 px. A map with 7 stations and drones does not fit 320 px in a way you can read.

➡️ Yes. Use the list of bays on phones and the full floor from about 1024 px wide.

---

❓ **Q4 - Real data only**: Should the floor show only signals that Tether records today? Some signals the prototype draws do not exist yet:
- Many backlog items have no stage. The README says those stages are samples.
- Review rounds are samples too.

Under this rule, an item without a stage goes to an "Unsorted" area at the floor entrance. The server work fills in the missing data (stage per item, review rounds over HTTP, tab states in one snapshot). The floor does not ship before that data exists.

➡️ Yes. A floor with made-up positions breaks your trust in it fast.

---

❓ **Q5 - Answering from the floor**: Should a beacon open the same full-screen answer view as the Queue row? That view uses the same approval path: bound to the tab, the conversation, the prompt and the choice. That path is the fix for spec v3 critical finding 01, where Friday can approve the wrong tab. The other choice is a separate quick-answer control on the floor.

➡️ Yes, the same answer view. A second answer path is how the wrong-tab bug came back before.

---

Questions about the look and the motion come after this round:
- Keep the night-shift look?
- 3D or flat?
- Sound?

That round needs you to have seen the prototype: https://claude.ai/artifact/XEDDEou1U4jZGfdjhGMeFA

Next: reply "C, yes to all", or give the numbers you want changed.

Needs you: yes