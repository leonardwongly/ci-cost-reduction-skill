# What OpenClaw did, and why it may not transfer

Source: "OpenClaw CI cost reduction", https://vincent.build/openclaw-ci-cost-reduction/
(read 2026-11-18), and the OpenClaw repo at commit
`a7d05c7af04aa17ac57c994da238d0c60f058ca6`. Items marked *(unverified)* come
from a summary of the page, not from text read directly. Check the linked PR
before relying on them.

## Context: why their numbers are theirs

- **Scale:** about 1.48M jobs and 158.5k runner-hours in their busiest week
  (17–23 Sep 2026); about 19,660 PR-triggered CI runs that week in the main
  repo (about 117 an hour). Much of the traffic came from coding agents.
- **Runners:** GitHub Actions orchestration on Blacksmith runners (later also
  boat.dev). Compute is sponsored, so dollar figures are *estimated list-price
  value* (Blacksmith usage × published prices), not an invoice.
- **Stack:** about 89% TypeScript, pnpm, Vitest, tsgo/tsrs type-checkers, plus
  iOS, Android, macOS, Windows and Rust lanes. Several techniques depend on this
  stack.
- **Headline results** (busiest week vs 6–13 Nov 2026): runner-hours −86.5%
  (158,546 → 21,395), jobs −79.7%, estimated compute value −95.5%
  ($1.27M → $57k per 30 days). Commit volume also fell about 44% over the same
  period, so not all of the reduction is efficiency. The author says so.
- **What they did not establish:** a PR-to-merge (feedback time) improvement.
  Their compute reduction is measured. Their speed gain is not.

Lesson for your audit: **normalise for volume** and keep cost and feedback time
as separate claims.

## Techniques, in the article's order

| # | Technique | Mechanism | Reported effect | Transfer check |
|---|---|---|---|---|
| 1 | Delete waste first | Test-line budget guard (PR #182230); redundant assertions removed with breakage proof (PR #191917) | Test lines −418k net; no CI-time number given | Only if test volume is growing faster than value; needs team agreement |
| 2 | Right stage for checks | PR = merge question; main push = small guards; schedule = broad validation; 90s admission delay on main (PR #95681 *(unverified)*); hourly CodeQL reusing unchanged categories 24h (PRs #191097, #191805 *(unverified)*) | Not separated | Needs a team that fixes a red main quickly |
| 3 | De-duplicate reviews | One automatic review per new PR; follow-ups on request | No cost figure | Applies if AI or bot review jobs run on every push |
| 4 | Affected-test selection | Deterministic graph planner; reasons recorded; null plan → broad run; shadow mode | Example PRs 4,018 → 15 test entries; Windows fan-out fix 2,962 → 181 targets (PR #195392 *(unverified)*) | Only with a trustworthy dependency graph and large test minutes |
| 5 | Local checks | Format, lint and focused tests before push; faster local type-checker (447s → 64s, PR #175711 *(unverified)*) | Fewer wasted CI rounds | Generally applies |
| 6 | Environment reuse | Blobless checkout; cache keys on lockfile + toolchain + platform; artifact split 55 → 30 MB (PR #195767 *(unverified)*); Vitest `isolate:false` −44% case time (PR #196117 *(unverified)*) | Per-lane gains | Generally applies; isolation change needs proof |
| 7 | Packing and balancing | Timing-based packing to a 300s budget; shards 95 → 63 (PR #95649 *(unverified)*); slowest job 403s → 300s (PR #104472 *(unverified)*) | Group time 6m43s → 5m00s | Applies to sharded suites |
| — | Runner sizing | Same 240 files: 32-class $1.152 / 17m54s vs 16-class $0.672 / 20m51s (PR #167021) | Cheaper but 16.5% slower | Always measure both axes |

## Required-check handling (read from ci.yml)

- One required aggregate job, `openclaw/ci-gate`, `needs` every lane and runs
  with `always()`.
- A `preflight` job emits `run_*` outputs. The gate pairs each lane's
  `needs.<lane>.result` with its `run_*` flag and accepts only `true:success`,
  `false:success` or `false:skipped`.
- Narrow, named exceptions (known-red main matching hourly main failures;
  coalesced hourly iOS) are coded explicitly with a notice. Everything else
  fails.
- PR concurrency uses `cancel-in-progress` on `pull_request` only. Main pushes
  keep validation slots. Drafts are isolated per run.
- `push` to main uses `paths-ignore` for `**/*.md` and `docs/**`. The PR
  trigger has no path filter, so the gate always reports.

## Verification approach they describe

Selector unit tests; backtesting against earlier regressions; deliberately
broken fixtures; shadow mode where dependency recording and model scoring ran
but could not skip tests. A richer learned model missed regressions the static
source graph caught, so they kept the deterministic graph. Daily estimated
compute $7,738 (5 Nov) → about $1,500 (7 Nov and 12 Nov), with several changes
overlapping, so the author doesn't credit any one change.

## Stated lessons

- Remove unnecessary work before buying faster machines.
- Cheaper runners can lengthen waits.
- More parallel jobs can repeat setup and raise cost.
- Deleting tests needs an explicit instruction in the definition of done.

## Likely *not* to transfer without evidence

- The planner's mappings and policy lists: entirely repo-specific.
- Numbers driven by agent-generated PR volume. A human-paced repo has far fewer
  superseded runs.
- Vendor-specific runner behaviour (Blacksmith or boat queueing, caching,
  pricing).
- Toolchain swaps (tsgo → tsrs, Oxlint): only relevant to the same stack.

## Links

- Article: https://vincent.build/openclaw-ci-cost-reduction/
- CI workflow: https://github.com/openclaw/openclaw/blob/a7d05c7af04aa17ac57c994da238d0c60f058ca6/.github/workflows/ci.yml
- Local proof and test-line budget: https://github.com/openclaw/openclaw/blob/a7d05c7af04aa17ac57c994da238d0c60f058ca6/docs/ci/local-proof.md
- Planner: `scripts/lib/ci-changed-node-test-plan.mts`, `scripts/test-projects.test-support.mts` (same commit)
- Timing refit: `.github/workflows/ci-test-timings-refit.yml`; nightly full validation: `.github/workflows/full-release-validation-nightly.yml`
- PRs: #167021, #182230, #191917 (read directly); others listed above are from the summary
