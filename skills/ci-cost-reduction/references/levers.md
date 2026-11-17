# Levers, from cheapest and safest to riskiest

For each lever: the evidence in the baseline that it applies, how to apply it,
the risk to correctness, and how to verify it. Snippets are for GitHub Actions.
Translate them for other systems. Check action versions against the current
releases before pinning.

## Contents
1. Cancel superseded runs
2. Skip work that can't be affected (docs-only, lane planning)
3. Run checks at the right stage
4. Environment reuse: checkout, install, caches, artifacts
5. Runner right-sizing
6. Packing, sharding and balancing
7. Fix flakiness and retries
8. Remove redundant tests and checks
9. Local pre-push checks
10. Affected-test selection (see affected-tests.md)

---

## 1. Cancel superseded runs
**Evidence:** several runs per PR head within minutes; minutes in runs whose
commit was replaced before they finished.
**How:**
```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```
Cancel only on PRs. On the default branch, cancelling can leave a commit
without any validation. OpenClaw cancels on PRs and adds a **90-second
admission delay** to main-push runs, so a superseded push cancels before it
takes a runner. Consider that only when main pushes are frequent.
**Risk:** low. Make sure the required gate doesn't treat a *superseded* run as
the final word. The newer run reports on the newer SHA.
**Verify:** cancelled-minutes and runs-per-PR fall. Required checks still
appear on the latest SHA.

## 2. Skip work that can't be affected
**Evidence:** docs-, markdown- or asset-only PRs running full test or build
lanes; lanes for platforms (iOS, Windows, Rust) running on unrelated changes.
**How:** a cheap planning job that works out which lanes are affected, plus the
gate pattern (`required-checks.md`). Don't put workflow-level `paths` on a
required workflow. Main-push workflows *can* use `paths-ignore` for docs if
nothing required hangs off them (OpenClaw does this for `**/*.md` and `docs/**`
on push). OpenClaw also skips a Rust proof job on TypeScript-only PRs.
**Risk:** medium. Wrong path lists skip real work. Keep the planner's "unknown
file means run everything" rule.
**Verify:** docs-only PR: lanes skipped, gate green. Code PR: lanes run.
Deliberate failure: gate red.

## 3. Run checks at the right stage
**Evidence:** expensive suites (cross-OS matrices, long e2e, full security
scans, release packaging) run on every PR push but rarely fail first there, or
duplicate what the merge queue or main run already does.
**How:** sort checks by the question they answer. "Can this merge?" belongs on
PRs. "Is main healthy?" belongs on main pushes or a schedule. "Is this
releasable?" belongs on release or nightly runs. OpenClaw runs hourly CodeQL on
main and reuses unchanged categories for up to 24 hours, and a scheduled job
does broader validation on a named main revision. Keep a manual or label
trigger so a risky PR can request the broad suite.
**Risk:** medium. A failure moves from "before merge" to "after merge". Agree
explicitly on who owns a red main and how fast it's fixed, and keep anything
security-critical on the PR if the team requires it.
**Verify:** the moved suites still run (schedule fires, results visible), and
the team agrees to the new failure-ownership model.

## 4. Environment reuse
**Evidence:** setup-like steps (checkout, toolchain setup, dependency install,
cache restore, docker pull, build-before-test) are a large share of job minutes.
`analyze` reports this under "Setup-like steps".
**How:**
- Dependency caches keyed on lockfile + toolchain version + OS/arch
  (`actions/setup-node` `cache:`, `actions/cache` with
  `hashFiles('**/pnpm-lock.yaml')`). A cache key missing the toolchain or
  platform silently restores incompatible artifacts.
- Shallow or blobless checkout (`fetch-depth: 1`, or `filter: blob:none` when
  history is needed for diffs).
- Build once, pass artifacts to test shards (`upload-artifact`/`download-artifact`)
  instead of rebuilding per shard. Measure archive size, because large artifacts
  can cost more time than they save. OpenClaw split one 55 MB archive into
  30 MB runtime plus 0.5 MB tooling.
- Prebuilt CI images or containers for heavy toolchains.
- Test-runner settings: OpenClaw got 44% less case-execution time in selected
  Vitest lanes with `isolate: false`. Only do that where tests don't share
  mutable global state. Prove it by running the lane several times and in a
  random order.
**Risk:** low to medium (stale or poisoned caches, test isolation). Never cache
across trust boundaries (fork PRs writing caches the default branch reads).
**Verify:** setup minutes per job fall, cache hit rate is visible in logs, and a
lockfile change still invalidates the cache.

## 5. Runner right-sizing
**Evidence:** per-job duration × per-minute rate by runner class. CPU-bound jobs
(compile, type-check, large test suites) on small runners, or I/O-bound jobs on
large ones.
**How:** run the same commit on two sizes, several times each. Compare **cost**
(minutes × rate) and **wall-clock**. OpenClaw example (PR #167021, 240 test
files): 32-class runner $1.152 in 17m54s; 16-class $0.672 in 20m51s. Cheaper,
but 16.5% slower. Which one is right depends on whether that job is on the PR
critical path. Self-hosted or third-party runners change the rate and the queue
behaviour. Include queue time in the comparison.
**Risk:** low for correctness, real for feedback time.
**Verify:** before and after on the same commits, with both p50 and p90.

## 6. Packing, sharding and balancing
**Evidence:** many shards each shorter than their setup; uneven shard durations
(the slowest shard sets wall-clock time); per-job minute rounding
(`billable_minutes` much bigger than `job_minutes`).
**How:** shard by recorded test timings, not file count (most runners support
timing-based splitting). Merge tiny jobs. Set a per-job time budget and pack to
it. OpenClaw packs to a 300-second budget, regenerates timings in a scheduled
workflow (`ci-test-timings-refit.yml`), cut 95 planned Node shards to 63, and
cut its slowest job from 403s to 300s.
**Risk:** low. Check that no test is dropped by the new split: the total count
must match.
**Verify:** total tests executed unchanged; shard spread (max − min) narrower;
billable minutes and wall-clock compared.

## 7. Fix flakiness and retries
**Evidence:** rerun rate, failed attempts that passed on rerun of the same SHA
(`analyze` → "Flake signal"), and jobs with intermittent failure rates.
**How:** quarantine known flakes visibly (a tracked list, still run but not
blocking, each with an owner), fix the top ones, and limit automatic retries to
one, logging each retry. Don't add blanket retries. They hide real failures and
double the cost.
**Risk:** quarantining can hide a real regression. Keep the list short and
reviewed.
**Verify:** rerun rate and failed-then-passed count fall. Quarantine list size
is tracked.

## 8. Remove redundant tests and checks
**Evidence:** the same lint or type-check run in several workflows; tests that
assert what another test already proves; slow tests with no independent signal.
**How:** remove duplicates one at a time. Before deleting a test, show that
another check still catches the same breakage: break the code on purpose and
confirm something still fails. OpenClaw added a "test-line budget" guard and
deleted about 418k net test lines over 6 weeks. In PR #191917 they removed a
redundant assertion and confirmed by deliberate breakage that the remaining
guard still fired. Deleting tests needs explicit agreement from the team.
**Risk:** medium to high. This is real coverage loss when wrong.
**Verify:** deliberate-breakage evidence for each removal. Note coverage before
and after where it's measured.

## 9. Local pre-push checks
**Evidence:** a large share of CI failures are format, lint or type mistakes
that would fail locally in seconds.
**How:** fast local commands (`make check`, `pnpm check:changed`, pre-commit
hooks). CI still enforces them. This only reduces wasted CI rounds. OpenClaw
moved local typecheck from tsgo (447s) to a faster checker (64s) and kept the
slower one for release validation.
**Risk:** none to correctness if CI still runs the checks.
**Verify:** share of CI failures from lint or format falls.

## 10. Affected-test selection
See `affected-tests.md`. Highest potential, highest risk, highest maintenance.
