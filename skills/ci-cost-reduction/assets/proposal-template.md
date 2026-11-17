# CI cost and feedback-time proposal: <repo>

**Status:** Draft, awaiting approval
**Baseline window:** <from> → <to> (<n> days, <runs> run attempts, <prs> PRs; sampled? <yes/no>)
**Rates used:** <source of rates; "estimated list price" or "billed">

## 1. Baseline summary
| | Value |
|---|---|
| Billable minutes (total / per PR / per active day) | |
| Estimated cost (total / per PR) | |
| PR wall-clock p50 / p90 (first attempt) | |
| Job queue p90 | |
| Cancelled + rerun + failed minutes | |
| Flake signal (failed→passed SHAs) | |
| Job types making up 80% of minutes | |

Where the minutes go: <the 3–5 jobs or workflows that dominate, with share %>.
What developers wait for: <the critical-path job, and why>.

## 2. Required checks (must keep working)
| Check name | Produced by (workflow / job) | Affected by proposal? | How it stays correct |
|---|---|---|---|

Merge queue: <yes/no>. Branch protection / rulesets read from: <API / provided by user>.

## 3. Proposed changes (ranked)
For each change:

### C-1 <short title>
- **Evidence:** <baseline numbers that show the problem>
- **Change:** <what will be edited>
- **Expected effect:** <minutes / cost / wall-clock, with the arithmetic>
- **Risk to correctness:** <low/med/high>: <how a failure could slip through> → <mitigation>
- **Verification:** <deliberate-breakage test, gate scenarios, metrics to compare>
- **Rollback:** <revert commit / flag / variable>
- **Effort:** <S/M/L>

## 4. Not recommended (and why)
- <OpenClaw technique or other idea> — <why the evidence here doesn't support it, or why the risk outweighs it>

## 5. Implementation order
1. C-… (one commit/PR each)

## 6. Acceptance criteria
- AC-1 Required checks report on normal, docs-only and conditional-skip PRs.
- AC-2 A deliberate failure in each newly conditional path turns the gate red.
- AC-3 Full-suite fallback triggers fire for: <list>.
- AC-4 Billable minutes per PR fall by ≥ <x>% over a comparable window.
- AC-5 PR wall-clock p90 doesn't get worse by more than <y>%.
