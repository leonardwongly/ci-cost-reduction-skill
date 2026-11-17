# Affected-test selection: safely

Running only the tests a change can affect is often the biggest single saving
on a large test suite. It is also the only lever on the list that can let a
real regression merge without anyone noticing. Treat it as a safety-critical
component: design it to fail closed, prove it in shadow mode, and keep a full
run behind it.

## Is it worth it here?

Go ahead only if **all** of these hold in the baseline:
- Test jobs account for a large share of CI minutes or of PR wall-clock time.
- Most PRs touch a small part of the codebase (check the changed-file
  distribution of recent PRs).
- A dependency graph exists that you can trust: a build system (Bazel, Nx,
  Turbo, Gradle, Pants, Go packages), TypeScript project references, or a
  static import graph that covers almost every file.
- Someone will maintain it. Selection logic decays as the repo changes.

If the repo is small, or tests already finish in a few minutes, cheaper levers
(caching, sharding balance, cancelling superseded runs) almost always win.
Say so.

Before writing your own, use what the stack already gives you:
`nx affected`, `turbo run --filter=...[origin/main]`, `bazel` with
`target-determinator` or `bazel-diff`, `gradle` build cache with configuration
avoidance, `pytest-testmon`, `jest --changedSince`/`vitest --changed`, `go list`
dependencies. Each has its own gaps. Check how it handles config files,
non-code assets and dynamic imports.

## How OpenClaw does it (for reference, not to copy)

OpenClaw's planner (`scripts/lib/ci-changed-node-test-plan.mts`,
`scripts/test-projects.test-support.mts`) maps a diff to tests through:
1. Direct test edits: a changed test runs itself.
2. Explicit owner mappings for links an import walk can't see (for example a
   shared config whose consumers reach it through an SDK facade).
3. Sibling and same-directory tests.
4. Reverse-import consumers from the static import graph.
5. Policy-watch targets: fixed tests attached to sensitive paths.
6. A fixed **smoke set** that always runs.

Every selected target records *why* it was selected (`explicit-owner`,
`import-consumer`, `policy-watch`, `fixed-smoke`), so a reviewer can audit the
selection. The planner returns "no plan" (`null`) on: missing or invalid changed
paths, a selected test with no executable owner, an unresolved related test
owner, or missing shard metadata. Their local change-lane router states the
rule plainly: unknown root or config changes fail safe to all lanes. Their
article reports example PRs going from 4,018 to 15 test entries. It also notes
that a richer learned model *missed regressions the plain source graph caught*,
so they kept the deterministic graph.

## Design rules

1. **Fail closed.** The selector's output is either a list of tests or
   "unknown". Unknown means the full suite. Every error path, exception,
   timeout and unexpected input leads to unknown.
2. **Full-suite triggers** (adapt the list to the repo, and keep it in code with
   a test):
   - Changes to build or toolchain config, lockfiles, the CI workflow itself,
     test configuration, test helpers, fixtures or shared mocks.
   - Changes to code generators, schemas, or anything other code reads at
     runtime without importing it (env files, JSON config, templates).
   - A changed file that the graph doesn't know about (new file type, generated
     path, file outside every project).
   - Dynamic imports, reflection, plugin loading or string-based registries the
     graph can't follow (or map them explicitly).
   - A diff that couldn't be computed: shallow clone without the merge base,
     force-push with an unknown base, a missing base ref. Use `fetch-depth: 0`
     or fetch the base explicitly. Never treat an empty diff as "no tests".
   - Selection larger than a threshold (for example over 40% of tests). Running
     everything is then simpler and nearly as cheap.
   - Merge queue, pushes to the default branch, release branches, scheduled
     runs, and a manual override (`CI_FULL=1` env, a label, or a `[full ci]`
     commit trailer).
3. **Always run a smoke set.** A small fixed set of cross-cutting tests guards
   against graph blind spots.
4. **Keep a full run behind it.** Run the full suite on main (on each merge, or
   on a schedule if that's too costly). When it finds a failure the PR selection
   missed, treat that as a selector bug: add the missing mapping and a
   regression test for the selector.
5. **Make it explainable.** Log which rule selected each test and which trigger
   forced a full run, and put that in the job summary.
6. **Test the selector itself.** Unit tests for each mapping rule and each
   full-suite trigger, including "invalid input means full".

## Rollout

1. **Shadow mode.** Compute the selection on every PR but still run the full
   suite. Record: selected set, full-suite failures, and whether every failing
   test was in the selected set. Run this for at least 2 weeks or about 100 PRs,
   whichever comes later.
2. **Backtest.** Take recent real failures (failed CI runs on PRs and main,
   reverted commits, bug-fix PRs with a regression test) and replay the
   selector on their diffs. Every one must select the failing test, or fall
   back to full. Report recall as caught/total with the list of misses.
3. **Deliberate breakage.** On a throwaway branch, break a leaf module, a
   widely imported shared module, a config file and a test helper. Confirm the
   expected tests are selected, or the full suite runs, and that the gate goes
   red.
4. **Enforce** only when shadow recall is 100% (or every miss is understood and
   fixed) and the user agrees. Keep the scheduled full run and the manual
   override.
5. **Watch** "misses caught by the main full run" after rollout. Each one is a
   bug in the selector, not just bad luck.

## What to report

- Tests executed per PR (p50/p90), before and after.
- Full-suite fallback rate and its top triggers. A very high rate means the
  selector isn't paying for itself. A very low rate on a monorepo may mean a
  trigger is missing.
- Shadow and backtest recall with sample size.
- Misses found by the main full run since enforcement.
