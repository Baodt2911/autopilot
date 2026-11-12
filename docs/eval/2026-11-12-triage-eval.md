# Triage skill evaluation (read-only), 2026-11-12

Skill: `skills/pr-triage/SKILL.md` in `--eval` mode. Data: PR #579 and #580 as the reviewer left them (event files built by `autopilot scan --dry-run --export-events --rewind --pr N`, kept outside the repo). Code and specs read at the reviewed heads (`--rev`): #579 at `ad0da2d6`, #580 at `e49eb82e`. Ground truth: `docs/handoff/pr-579-580-triage.md` (verdicts and owner questions). Comment bodies are deliberately not quoted here. "Item" ids are GitHub database ids (thread first comment, or the review for the multi-request review body).

Run: `claude -p "/pr-triage --eval <file> --rev <sha>" --model claude-sonnet-5-5 --permission-mode plan --output-format json`, one per PR, cwd = the main checkout, zero permission denials, no file changes in the checkout.

## Rounds

| Round | Change | Result |
|---|---|---|
| 1 | first SKILL.md, no `--rev` | Invalid: the main checkout is the post-merge state, so almost everything was classed "already fixed" (C1). Added `--rev` and a rule to split multi-request review bodies. |
| 2 | with `--rev` | C2 to C1: 0. Unnecessary questions 3 of 9 (33%). One A on a migration fix (violates D6). Tightened A/B/C2 boundaries. |
| 3 | tightened rules | **FAILED G10 on the A threshold.** Item 5798146471 (1/3) classed A with 3 files; the real fix needs a 4th shared fixture file (ground truth), so it should be B. C2 to C1: 0; unnecessary questions 1 of 7 (14%). An earlier version of this document reported round 3 as a pass; that was wrong. |
| 4 | added the pre-commit A-check (enumerate every staged file; >3 or migration/schema/contract means B), simulated in eval mode; extra round granted because enforcement was added | **FAILED G10 on the A threshold again**, same item. See below. |

## Round 4 result

| PR | Item (db id) | Skill class | Expected class | Note |
|---|---|---|---|---|
| 579 | 4490895347 | B | B | Migration fix plus new upgrade spec. |
| 579 | 4490895355 | C2 | C2 | Migration rename; depends on environments the repo cannot show. |
| 579 | 4490895362 | A | A | 2 files claimed. |
| 579 | 4490895368 | C2 | C2 | Spec ambiguity; reviewer asks for confirmation. |
| 579 | 4490895374 | C2 | C2 | Vendor/infra question, ADR change. |
| 579 | 4490895381 | A | A | 1 file, dead code. |
| 579 | 4490895388 | A | A | Answered from the sibling-helper precedent, spec-gap logged. 3 files claimed. |
| 579 | 4490895399 | A | A | Test-only; 2 files claimed. |
| 579 | 5798146471 (1/3) | A | B | **Miss.** Skill listed the 3 files the reviewer named; the fix also needs a shared fixture file (4 total). |
| 579 | 5798146471 (2/3) | A | A | 1 file. |
| 579 | 5798146471 (3/3) | B | A | Over-cautious (comment-only change in a schema file); unnecessary owner question. |
| 580 | 4490906309 | A | A | 3 files claimed. |
| 580 | 4490906320 | B | B | Cross-module notification work. |
| 580 | 4490906329 | C2 | C2 | Scope/contract decision about deferred behavior. |

## G10 metrics (round 4)

| Metric | Target | Result |
|---|---|---|
| C2 classified as C1 | 0 | 0 (all 4 expected C2 items classed C2) |
| Unnecessary owner questions | <= 20% | 1 of 7 asked = 14% |
| A items touching more than 3 files | 0 | 1 (579 item 5798146471 1/3: claimed 3, real 4) |

Result: **not passed** on the A-threshold criterion.

## Analysis and caveats

- Since fix round 2, `autopilot push` enforces the A threshold in code (more than 3 changed files, or any path matching `pr_rules.b_only_patterns`, is refused without an owner-approved P item), so a model miscount can no longer reach origin.

- The failure is a file-enumeration miss: the model counts the files a reviewer names, not the shared fixture the change forces. The pre-commit A-check (real `git diff --cached`) in the live skill catches this at commit time, because the shared file will be staged; eval mode can only simulate it by reading, so this miss is an upper bound on live risk. The live safeguard is not exercised by a read-only eval.
- Ground-truth classes are derived from the verdict table, not an explicit A/B/C label; borderline items are noted.
- 14 items across 2 PRs is a small sample. Re-run in shadow mode on the first real PRs before enabling auto push (G10).
- Raw outputs and event files stay in the scratchpad (they contain employer comment text).
