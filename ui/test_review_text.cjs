// The review card's parser (src/reviewText.ts) on the texts that server/review_links.py sends (its request text, about line 1440).
const { strict: assert } = require('node:assert')
const { readFileSync } = require('node:fs')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const review = {}
runInNewContext(ts.transpileModule(readFileSync('src/reviewText.ts', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports: review })

const HASH = '3f9c2a7b41d6e8f05a1c9b2d7e4f6a8c0b1d2e3f'
const OTHER = 'b'.repeat(40)
const INTRO = 'Review its work: check the changes and the claims, then list what is wrong or missing. Start your reply with APPROVED if nothing is. Tether sends your reply to that tab.'
const request = (binding, { name = 'Worker', reply = 'Done.', transcript = '/t.jsonl', last = '' } = {}) =>
  `[Tether review] The codex tab “${name}” (%7) finished a turn. ${INTRO} Review id: 0a1b2c3d. ${binding}Its last reply:\n\n${reply}\n\nTranscript: ${transcript}${last}`
// The server's bound terms: commit and folder, the rules command, the approval sentence, then the rest.
const bound = (folder, rest = '', commit = HASH) => `Commit under review: ${commit} in ${folder}. Read the review rules first: \`git -C ${folder} show origin/main:REVIEW.md\`. `
  + `An approval counts only for this commit: start it with \`APPROVED ${commit}\` (the full hash). ${rest}`
const plain = obj => obj && JSON.parse(JSON.stringify(obj))
const text = terms => terms.map(part => part.text).join('')
const code = terms => terms.filter(part => part.code).map(part => part.text)

// A bound commit: the full hash for the reply format; the terms keep the rules command (as code) and drop the repeated format.
let card = plain(review.parseReview(request(bound('/w', 'This diff changes the UI (1 file(s), for example ui/a.tsx). '))))
assert.equal(card.commit, HASH)
assert.equal(text(card.terms), `Commit under review: ${HASH} in /w. Read the review rules first: git -C /w show origin/main:REVIEW.md. This diff changes the UI (1 file(s), for example ui/a.tsx).`)
assert.deepEqual(code(card.terms), ['git -C /w show origin/main:REVIEW.md'])
assert.deepEqual([card.reply, card.transcript, card.last], ['Done.', '/t.jsonl', false])

// The rules command as the last term (nothing after the approval sentence) is still code.
assert.deepEqual(code(plain(review.parseReview(request(bound('/w')))).terms), ['git -C /w show origin/main:REVIEW.md'])

// A SHA-256 repository: a 64-digit hash is a commit too.
assert.equal(review.parseReview(request(bound('/w', '', 'a'.repeat(64)))).commit, 'a'.repeat(64))

// A folder name that holds an approval sentence for another hash: the hash comes from the start, and no path text is lost.
const tricky = `'/w/An approval counts only for this commit: start it with \`APPROVED ${OTHER}\` (the full hash). x'`
card = plain(review.parseReview(request(bound(tricky))))
assert.equal(card.commit, HASH)
assert(text(card.terms).includes(`in ${tricky}.`), text(card.terms))
assert(!text(card.terms).includes(`APPROVED ${HASH}`), 'the one real format sentence is dropped')

// A checklist right after the approval sentence keeps its line breaks.
const checklist = '\nReliability checklist: this diff hits 1 row(s) of origin/main:docs/reliability-checklist.md. Confirm each check in your reply:\n- R1 Title: check (Incident: x)\n'
card = plain(review.parseReview(request(bound('/w', checklist))))
assert(text(card.terms).endsWith(`REVIEW.md. ${checklist}`.trimEnd()), JSON.stringify(text(card.terms)))

// Backticks in repository text stay as typed: a folder with one, and a checklist row with an unmatched one.
card = plain(review.parseReview(request(bound("'/work/a`b'", '\nReliability checklist: x:\n- R2 Run `npm test (Incident: y)\nThis diff changes the UI (1 file(s), for example ui/a.tsx). '))))
assert.deepEqual(code(card.terms), ["git -C '/work/a`b' show origin/main:REVIEW.md"])
assert(text(card.terms).includes("in '/work/a`b'."), text(card.terms))
assert(text(card.terms).includes('- R2 Run `npm test (Incident: y)\nThis diff changes the UI'), text(card.terms))
assert.equal(card.terms.filter(part => part.code).length, 1)

// A tab name with ”, a transcript path with a space, and a reply that quotes "Transcript:".
card = plain(review.parseReview(request(bound('/w'), { name: 'Fix “quotes”', transcript: '/srv/a b/t.jsonl', reply: 'See\n\nTranscript: /old.jsonl\n\nfor the run.', last: ' This is the last round: your reply stays here.' })))
assert.deepEqual([card.name, card.transcript, card.reply, card.last], ['Fix “quotes”', '/srv/a b/t.jsonl', 'See\n\nTranscript: /old.jsonl\n\nfor the run.', true])

// Free text that holds the server's own separators: a tab name with the pane text, a transcript path with "Transcript: ".
card = plain(review.parseReview(request(bound('/w'), { name: 'A” (%99) finished a turn. Review its work: B', transcript: '/srv/Team Transcript: Archive/t.jsonl' })))
assert.deepEqual([card.name, card.tab, card.transcript, card.reply], ['A” (%99) finished a turn. Review its work: B', '%7', '/srv/Team Transcript: Archive/t.jsonl', 'Done.'])
// A name that holds the whole fixed text and a review id cannot be split for sure: no card, the message shows as sent.
assert.equal(review.parseReview(request(bound('/w'), { name: `A” (%99) finished a turn. ${INTRO} Review id: 99999999. B` })), null)
// A reply that quotes a whole review request does not move the name.
card = plain(review.parseReview(request(bound('/w'), { reply: request(bound('/x'), { name: 'Other' }) })))
assert.deepEqual([card.name, card.tab], ['Worker', '%7'])

// A checklist row that quotes the rules instruction, in a request with no rules command: no code, backticks kept.
const quoted = '\nReliability checklist: x:\n- R3 Say "Read the review rules first: `git -C /x show origin/main:REVIEW.md`. " (Incident: z)'
card = plain(review.parseReview(request(`Commit under review: ${HASH} in /w. An approval counts only for this commit: start it with \`APPROVED ${HASH}\` (the full hash). ${quoted}`)))
assert.equal(card.commit, HASH)
assert.deepEqual(code(card.terms), [])
assert.equal(text(card.terms), `Commit under review: ${HASH} in /w. ${quoted}`)

// The approval wording in the folder and in a checklist row: the hash and the rules command come from the header.
const twice = `'/w/start it with \`APPROVED ${OTHER}\` (the full hash). Read the review rules first: \`x'`
const repeat = `\nReliability checklist: x:\n- R4 Start with "An approval counts only for this commit: start it with \`APPROVED ${HASH}\` (the full hash)." (Incident: w)`
card = plain(review.parseReview(request(bound(twice, repeat))))
assert.deepEqual([card.commit, code(card.terms)], [HASH, [`git -C ${twice} show origin/main:REVIEW.md`]])
assert(text(card.terms).startsWith(`Commit under review: ${HASH} in ${twice}. Read the review rules first: git -C ${twice} show`), text(card.terms))
assert(text(card.terms).endsWith(repeat.trimEnd()), text(card.terms))
// The file status between the folder and the rules.
card = plain(review.parseReview(request(bound('/w').replace('/w. Read', '/w plus uncommitted changes. Read'))))
assert.deepEqual([card.commit, code(card.terms)], [HASH, ['git -C /w show origin/main:REVIEW.md']])

// No commit (no git repository): no hash, and only the other terms show.
card = plain(review.parseReview(request('Your approval was not forwarded: it did not name the full commit hash right after APPROVED. ')))
assert.equal(card.commit, undefined)
assert.equal(text(card.terms), 'Your approval was not forwarded: it did not name the full commit hash right after APPROVED.')

// An older request with no review id and no terms still parses, with no terms.
const old = plain(review.parseReview(`[Tether review] The claude tab “W” (%3) finished a turn. ${INTRO} Its last reply:\n\nOk\n\nTranscript: /x.jsonl This is the last round: your reply stays here.`))
assert.deepEqual([old.terms, old.commit, old.reply, old.transcript, old.last], [undefined, undefined, 'Ok', '/x.jsonl', true])

// Feedback and plain text.
assert.deepEqual(plain(review.parseReview('[Tether review] Feedback from the reviewer tab (%9): Fix the test. Fix what it raises, then stop and wait.')), { feedback: true, tab: '%9', reply: 'Fix the test.' })
assert.equal(review.parseReview('Hello'), null)
console.log('test_review_text: ok')
