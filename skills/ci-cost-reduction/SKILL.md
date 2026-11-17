---
name: ci-cost-reduction
description: Audit a repository's CI pipeline, measure a baseline (compute minutes, cost, PR feedback time, queue time, flakiness), propose the highest-value cost and speed changes, then implement them as small reviewable steps that keep required checks working and run the full test suite whenever affected-test selection is unsure. Verifies correctness, feedback time and compute cost before and after. Draws on the OpenClaw CI cost-reduction case study (vincent.build/openclaw-ci-cost-reduction) but checks every lesson against this repo's own data. Use this whenever the user wants CI to be cheaper, faster or less wasteful, or mentions GitHub Actions minutes or bills, slow PR checks, runner costs, CI queue time, flaky or rerun-heavy pipelines, sharding, caching, path filters, "only run affected tests", test impact analysis, merge queues, or making CI "only run what changed", even if they don't say "cost reduction".
---

# CI cost reduction

You are helping a team spend less compute on CI and get PR feedback sooner,
**without letting a broken change merge**. The order matters: correctness, then
feedback time, then cost. A cheaper pipeline that misses a failure is a
regression, whatever the bill says.

This skill is based on OpenClaw's write-up (see
`references/openclaw-playbook.md`). OpenClaw cut estimated compute value by
about 95% and runner-hours by about 86%. Their numbers describe their repo:
roughly 1.5M jobs a week, sponsored Blacksmith runners, a pnpm/Vitest/TypeScript
monorepo with iOS, Android and Rust lanes. Your repo will be different. Treat
each OpenClaw technique as a hypothesis. Act on it only when this repo's
baseline shows the problem it fixes.

## The workflow

Work in six phases. Phase 4 ends with a hard stop for approval. CI changes affect
everyone who merges, so the team decides what lands. If the user's own
instructions define a spec or approval process (for example spec-driven
development with `specs/<NNN>-…/`), the Phase 4 proposal is that spec and plan.
Write it in their format.

### Phase 1: Inventory the CI as it is

Read before you measure, so you know what the numbers mean.

1. Find every pipeline definition: `.github/workflows/*`, `.gitlab-ci.yml`,
   `.circleci/`, `buildkite/`, `Jenkinsfile`, `azure-pipelines.yml`,
   `.drone.yml`, plus any scripts they call (`scripts/ci/`, `Makefile`,
   `package.json` scripts, `turbo.json`, `nx.json`, Bazel and Gradle config).
2. For each workflow, record its triggers, path filters, concurrency or
   cancellation settings, jobs and their `needs` graph, matrix size, runner
   labels or sizes, timeouts, caches, artifact passing, and whether it is
   reusable or called.
3. **List the required checks before you change anything.** This is the list you
   must not break. On GitHub:
   ```bash
   gh api repos/{owner}/{repo}/branches/{branch}/protection --jq '.required_status_checks'
   gh api repos/{owner}/{repo}/rulesets --jq '.[].id' | xargs -I{} gh api repos/{owner}/{repo}/rulesets/{}
   gh api repos/{owner}/{repo}/commits/{sha}/check-runs --jq '.check_runs[].name'   # names as reported
   ```
   Check whether a merge queue is in use (`merge_group` trigger, ruleset
   `merge_queue`). Map each required check name to the job that produces it.
   The check's name is the job's `name:` (or the matrix-expanded name), not the
   workflow filename. If you can't read protection settings (403), say so and
   ask the user for the list. Don't guess.
4. Note how the tests are organised: test runner, how tests are discovered, and
   whether a dependency graph exists (TypeScript project references, Nx, Turbo,
   Bazel, Gradle, Go packages, Python imports). This decides how safe
   affected-test selection can be.

Write the inventory down (`ci-audit/inventory.md` or the user's spec folder).
It is evidence for the proposal.

### Phase 2: Measure a baseline

Opinions about slow CI are often wrong about *where* the time goes. Measure.

For GitHub Actions, use the bundled script. It uses only the Python standard
library and needs a token with `actions:read`.

```bash
python3 <skill-dir>/scripts/ci_baseline.py fetch --repo OWNER/REPO \
    --since 2026-10-20 --until 2026-11-03 --out ci-audit/baseline-jobs.jsonl
python3 <skill-dir>/scripts/ci_baseline.py analyze ci-audit/baseline-jobs.jsonl \
    --rates ci-audit/rates.json --required "build,test (ubuntu)" \
    --out ci-audit/baseline.json --md ci-audit/baseline.md
```

- Use at least 14 days (two full weeks, so weekday effects cancel out) and a
  window that ends before you change anything. Note releases, holidays or
  incidents in the window. On very busy repos, use `--max-runs` or `--sample`
  to stay within API rate limits, and record that it was sampled.
- Run `analyze` once **without** `--rates` and read the "Runner labels" table.
  Repos often use custom or third-party labels (`depot-ubuntu-22.04-16`,
  `github-ubuntu-24.04-x86_64-32`, `blacksmith-8vcpu-…`, `namespace-profile-…`)
  that a generic rule would price as a 2-core runner. Then copy
  `assets/rates.example.json` to `ci-audit/rates.json`, add a rule for every
  label set you saw (most specific first), and **set the rates to what this
  team actually pays.** Re-run with `--rates` and check the "Priced as" column. Check the provider's current price page,
  their plan's included minutes, and any self-hosted runner costs (machines
  aren't free: amortise them). If you don't know the rates, report minutes and
  runner-minutes by runner class, and label any dollar figures as estimates at
  list price.
- For other CI providers, write a small adapter that produces the same JSONL
  records (schema in `references/measurement.md`), then run `analyze` on it.
  The analysis and the before/after comparison work unchanged.

Read `references/measurement.md` for what each metric means, the pitfalls
(GitHub rounds each job up to the whole minute, concurrency-cancelled minutes,
rerun attempts, fork PRs), and how to measure locally when there's no API access.

The baseline should answer these questions:
- **Cost:** total and billable runner-minutes, estimated cost, cost per PR, split
  by workflow, job and runner class. Which few jobs carry most of the cost?
- **Feedback time:** PR wall-clock from push to all checks done (p50/p90), time
  waiting in the queue, and the critical-path job.
- **Waste:** minutes in cancelled runs, reruns, failed attempts that passed on
  retry (flakiness), docs-only changes running the full suite, setup steps
  (checkout, install, toolchain, cache restore) as a share of job time.

### Phase 3: Diagnose and rank opportunities

Go through `references/levers.md` in order. It lists the levers roughly from
cheapest and safest to riskiest. For each lever, check the evidence in this repo:

| Lever | Evidence that it applies here |
|---|---|
| Cancel superseded runs | Many cancelled or overlapping runs per PR; pushes in quick succession |
| Skip irrelevant work (docs-only, path-scoped lanes) | Docs- or config-only PRs running full suites |
| Move broad checks to the right stage | Expensive suites (e2e, cross-OS, security scans) on every PR push that rarely fail first there |
| Environment reuse (cache, install, checkout) | Setup steps are a large share of job minutes |
| Runner right-sizing | CPU-bound jobs on small runners, or idle large runners; check cost × time |
| Packing and sharding balance | Matrix jobs much shorter than setup; uneven shard times; per-job minute rounding |
| Affected-test selection | Test jobs dominate minutes **and** a trustworthy dependency graph exists |
| Remove redundant tests and checks | The same thing is checked twice; slow tests with no independent signal |
| Fix flakiness | Rerun rate is high; failures that pass on retry |
| Local pre-push checks | Many CI failures are lint or format mistakes caught late |

Estimate each candidate's value from the baseline: minutes saved × rate, plus
feedback time saved × PR volume. Then rate its **risk to correctness** (could it
let a failure through?) and its **effort and reversibility**. Prefer changes that
are high-value, low-risk and easy to revert. A 20% saving you can trust beats a
60% saving that needs a new test-selection system you can't validate yet.

Be explicit when OpenClaw's result is unlikely to transfer. For example, if
total CI is 2,000 minutes a month, a test-selection planner probably costs more
in maintenance than it saves, while `cancel-in-progress` and a dependency cache
might take an afternoon.

### Phase 4: Propose, then stop for approval

Use the template in `assets/proposal-template.md`. It should contain:
- The baseline summary with numbers and the measurement window.
- The required-checks list and how each proposed change keeps them working.
- The ranked changes, each with: expected effect (with the evidence behind it),
  risk and how it's mitigated, how you will verify it, and how to roll it back.
- An order of implementation: one lever per step, safest first.
- What you are **not** recommending, and why.

Present a short summary to the user and **wait for approval**. Don't edit CI
files before then. The user may approve only some of the changes. Implement only
those.

### Phase 5: Implement in reviewable steps

Each step is one lever, one commit (or one PR if the user wants PRs), small
enough to review in a few minutes. For each step:

1. Make the change. Keep job names that are required checks **exactly** as they
   are, unless the user has agreed to update branch protection at the same time.
   Renaming a required job leaves PRs stuck on "Expected — Waiting for status".
2. Follow `references/required-checks.md` whenever a change makes a job
   conditional (path filters, `if:` conditions, selection). Conditional jobs are
   the most common way cost work breaks merging. Some skipped checks block
   merging forever. Others count as success and hide real failures.
3. For affected-test selection, follow `references/affected-tests.md`. In short:
   **fail closed**. Run the full suite whenever the selector isn't sure: a
   build, lockfile, CI or test-infrastructure change, an unmapped file, a diff it
   couldn't compute, a merge queue or main-branch run, or a selection above a
   size threshold. Run in shadow mode (select, but still run everything and
   compare) before enforcing. Keep a scheduled full run on the default branch.
4. Validate the workflow syntax locally where tools exist (`actionlint`,
   `gitlab-ci-lint`, `circleci config validate`). Run the affected scripts and
   tests locally.
5. Write the commit message so it says what changed, why (quoting the baseline
   evidence), the expected effect, and how to revert.
6. Don't push, open PRs or change branch protection or rulesets without the
   user's explicit go-ahead. Those reach the whole team.

### Phase 6: Verify correctness, feedback time and cost

Claim only what you measured. Report each item as met, not met or not yet
measurable.

**Correctness (check this first, every step):**
- Every required check still reports on a normal PR, on a docs-only PR, and on
  a PR that skips the conditional lanes. No check is left "Expected".
- **Deliberately break something** on a throwaway branch (a failing assertion, a
  lint error, a type error in a file the selector should catch) and confirm the
  required gate goes red. Do it for each new skip or selection path. A gate that
  can't fail proves nothing.
- For test selection: compare selected versus full on shadow runs, backtest
  against recent real failures (`references/affected-tests.md`), and confirm the
  fallback triggers fire.
- Test counts: the full-suite path still runs the same number of tests as the
  baseline, within explained differences.

**Feedback time and cost:**
- After enough real runs (aim for the same window length as the baseline, or at
  least about 30 PR runs if the team wants an early read), run `fetch` and
  `analyze` again and then compare:
  ```bash
  python3 <skill-dir>/scripts/ci_baseline.py compare ci-audit/baseline.json ci-audit/after.json --md ci-audit/compare.md
  ```
- Compare **normalised** metrics (per PR, per run, per active day) as well as
  totals, because PR volume changes from week to week. Look at p90 as well as
  p50, because cost cuts often make the slow tail worse (smaller runners, more
  queueing). If the change can't be isolated (several changes landed together,
  volume shifted), say so rather than attributing the gain to one change.
- Where you can't wait for real traffic, measure directly: run the old and new
  pipeline on the same commit a few times and report each run.

### Final report

Write `ci-audit/report.md` (or update the spec) with: baseline versus after,
each step's verified effect, the status of each acceptance criterion with
evidence, what is still unverified, what you'd watch over the next few weeks
(flake rate, misses caught by the scheduled full run), and the rollback for each
change.

## Principles

- **Measure, then change.** Every proposal cites a number from this repo.
- **Remove work before you buy faster machines.** OpenClaw's biggest wins came
  from not running work at all. Faster or cheaper runners shift cost and time
  around. A smaller runner can be cheaper per job and still slow down feedback.
- **More parallelism isn't free.** Each extra job repeats setup and, on
  per-minute billing, rounds up again. Check cost as well as wall-clock time.
- **Uncertainty means the full suite.** A selector that is unsure of a change
  runs everything. Each skip has a scheduled full run behind it.
- **Small, reversible, attributable.** One lever per step, measured on its own
  where possible.
- **Honest numbers.** Report the sample size and the measurement window, say when
  the data is noisy, and keep estimated dollar figures apart from billed ones.
