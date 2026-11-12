# Triage skill evaluation (read-only), 2026-11-12

Skill: `skills/pr-triage/SKILL.md` in `--eval` mode. Data: PR #579 and #580 as the reviewer left them (event files built by `autopilot scan --dry-run --export-events --rewind --pr N`, kept outside the repo). Code and specs read at the reviewed heads (`--rev`): #579 at `ad0da2d6`, #580 at `e49eb82e`. Ground truth: `docs/handoff/pr-579-580-triage.md` (verdicts and owner questions). Comment bodies are deliberately not quoted here. "Item" ids are GitHub database ids (thread first comment, or the review for the multi-request review body).

Run: `claude -p "/pr-triage --eval <file> --rev <sha>" --model claude-sonnet-5-5 --permission-mode plan --output-format json`, one per PR, cwd = the main checkout, zero permission denials, no file changes in the checkout.

## Rounds

| Round | Change | Result |
|---|---|---|
| 1 | first SKILL.md, no `--rev` | Invalid: the main checkout is the post-merge state, so almost everything was classed "already fixed" (C1). Eval needs code at the reviewed commit. Added `--rev` and a rule to split multi-request review bodies. |
| 2 | with `--rev` | C2 to C1 errors: 0. Unnecessary questions: 3 of 9 asked (33%). A on a migration fix (violates D6). Tightened A/B/C2 boundaries in SKILL.md. |
| 3 | tightened rules | Table below. Meets G10. Max rounds (3) used. |

## Round 3 result

| PR | Item (db id) | Skill class | Expected class | Note |
|---|---|---|---|---|
| 579 | 4490895347 | B | B | Migration fix plus new upgrade spec. Owner must confirm no environment applied the old file. |
| 579 | 4490895355 | C2 | C2 | Migration rename; depends on environments the repo cannot show. |
| 579 | 4490895362 | A | A | 3 files (repository, mapper, detail spec). |
| 579 | 4490895368 | C2 | C2 | Spec ambiguity; reviewer asks for confirmation. |
| 579 | 4490895374 | C2 | C2 | Vendor/infra question, ADR change. |
| 579 | 4490895381 | A | A | 1 file, dead code. |
| 579 | 4490895388 | A | A | Answered from the sibling-helper precedent, spec-gap logged. |
| 579 | 4490895399 | A | A | Test-only, 1 file. |
| 579 | 5798146471 (item 1/3) | A | B (A if reviewer-named files only) | Skill counted the 3 files the reviewer named; ground truth needs a 4th shared fixture file. Under-count, see metrics. |
| 579 | 5798146471 (item 2/3) | A | A | 1 file. |
| 579 | 5798146471 (item 3/3) | B | A | Over-cautious: docs refresh plus a comment in a schema file; unnecessary owner question. |
| 580 | 4490906309 | A | A | Service plus its tests, 3 files. |
| 580 | 4490906320 | B | B | Cross-module notification work; plan needed. |
| 580 | 4490906329 | C2 | C2 | Scope/contract decision about deferred behavior. |

(The LGTM review bodies are noop and were not asserted; #580's empty COMMENTED review was classed noop.)

## G10 metrics (round 3)

| Metric | Target | Result |
|---|---|---|
| C2 classified as C1 | 0 | 0 (4 expected C2 items, all 4 classed C2) |
| Unnecessary owner questions | <= 20% | 1 of 7 asked = 14% (item 3/3 of the multi-request review) |
| A items touching more than 3 files | 0 | 0 claimed. Caveat: 579 item 1/3 is a true 4-file fix, claimed as 3. |

Other: no A on a migration or schema in round 3; no event decided by the skill that the ground truth sends to the owner (the 4 C2 items and the migration items all went to the owner).

## Caveats

- Ground truth classes are derived from the verdict table, not from an explicit A/B/C label; borderline items are noted.
- Only 14 items across 2 PRs; a pass here is weak evidence. Re-run on the first real PRs in shadow mode before enabling auto push (G10).
- Raw outputs and event files stay in the scratchpad (they contain employer comment text).
