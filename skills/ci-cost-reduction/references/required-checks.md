# Keeping required checks working

Cost work makes jobs conditional, and conditional jobs interact with branch
protection in ways that aren't obvious. Get one wrong and either every PR is
blocked, or worse, a failing PR becomes mergeable. Read this before you make any
job conditional.

The behaviour below describes GitHub Actions as of late 2026. Platforms change,
so check the current docs ("Troubleshooting required status checks", "Handling
skipped but required checks") and **test it on a throwaway branch**. Don't rely
on this file alone.

## How skips behave on GitHub

| Situation | What the required check shows | Effect |
|---|---|---|
| Whole workflow didn't run: `paths`/`paths-ignore`/`branches` filter, `[skip ci]` | No check is created; it stays **Pending / "Expected"** | PR **blocked forever** |
| Job skipped by its own `if:` that evaluated false | Check concludes **skipped**, which counts as passing | Mergeable (fine if the skip was intended) |
| Job skipped because a job in its `needs` failed (and it has no `always()`) | **skipped**, which counts as passing | **Danger:** an upstream failure can make the PR mergeable when the required check is the downstream job |
| Job renamed, or matrix values changed | The old name is never reported | PR blocked ("Expected — Waiting for status") |
| Merge queue enabled, but the workflow lacks the `merge_group` trigger | The check never reports in the queue | Queue stalls |
| Two workflows or apps report the same check name | Whichever reports last wins, unless the ruleset pins the source app | Confusing or unsafe |

Two consequences:
1. **Never put a workflow-level `paths` filter on a workflow that produces a
   required check.** Run the workflow always. Decide inside it (in a cheap
   `changes`/`preflight` job) which lanes run.
2. **Don't make conditional lanes themselves required.** Require one gate job
   that sees every lane.

## The gate pattern (preferred)

Make one aggregating job the required check, and have it check every lane's
result against whether that lane was *meant* to run. OpenClaw's `ci-gate`
(`.github/workflows/ci.yml` at commit `a7d05c7`) does exactly this. Each lane
reports `result|selected`, and the gate accepts only:

- `selected=true` → `success`
- `selected=false` → `success` or `skipped`

Anything else fails, including a selected lane that was `skipped` (meaning an
upstream failure), `cancelled` or `failure`. This is the key point: a skip
counts as success only when the planner *decided* not to run that lane.

Minimal version:

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      backend: ${{ steps.plan.outputs.backend }}
      frontend: ${{ steps.plan.outputs.frontend }}
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - id: plan
        run: ./scripts/ci/plan-lanes.sh   # writes backend=true|false etc.; any error => all true

  backend-tests:
    needs: changes
    if: needs.changes.outputs.backend == 'true'
    # ...

  frontend-tests:
    needs: changes
    if: needs.changes.outputs.frontend == 'true'
    # ...

  ci-gate:                       # <- the ONLY required check
    name: ci-gate
    if: always()                 # must run even when lanes fail or skip
    needs: [changes, backend-tests, frontend-tests]
    runs-on: ubuntu-latest
    steps:
      - env:
          RESULTS: |
            changes=${{ needs.changes.result }}|true
            backend-tests=${{ needs.backend-tests.result }}|${{ needs.changes.outputs.backend }}
            frontend-tests=${{ needs.frontend-tests.result }}|${{ needs.changes.outputs.frontend }}
        run: |
          fail=0
          while IFS= read -r line; do
            [ -n "$line" ] || continue
            name=${line%%=*}; rest=${line#*=}; result=${rest%%|*}; selected=${rest#*|}
            case "$selected:$result" in
              true:success|false:success|false:skipped) echo "ok   $name $result (selected=$selected)";;
              *) echo "::error::$name: $result (selected=${selected:-missing})"; fail=1;;
            esac
          done <<< "$RESULTS"
          exit $fail
```

Notes:
- `if: always()` is essential. Without it the gate is skipped when a lane fails,
  and a skipped required check passes.
- If `changes` itself fails, its outputs are empty, so `selected` is `missing`.
  The case statement rejects that. Keep it that way.
- Decide whether a `cancelled` run should pass. Normally no. OpenClaw allows a
  few narrow, named exceptions (a known-red main, a coalesced hourly iOS job).
  Add exceptions only with a written reason.
- Off-the-shelf alternative: `re-actors/alls-green` implements a similar check
  with `allowed-skips`. Its default treats any skip in the allowed list as
  fine, so it doesn't tell "intended" from "caused by a failure" unless you
  combine it with planner outputs.
- Cutover: add the gate, let it run for a while next to the existing required
  checks, then switch branch protection to require the gate. Only then make
  lanes conditional. Changing protection is a team-visible action, so get
  explicit approval.

## The "skip-twin" pattern (when you can't add a gate)

If the team must keep per-lane required checks, add a twin workflow with the
same job `name:` that runs only on the inverse paths and exits 0. It works, but
it's fragile: the two path lists drift apart, and a twin can cover up a real
skip. Prefer the gate.

## Checklist for every conditional change

- [ ] Required check names before and after are identical (compare `check-runs`
      on a sample commit).
- [ ] No workflow that produces a required check has a workflow-level
      `paths`/`branches` filter that can skip it on PRs.
- [ ] The `merge_group` trigger is present if a merge queue is used, and the
      queue runs the **full** suite (selection is for PR iteration, not the
      final merge check, unless the team agrees otherwise).
- [ ] The gate is tested on: a normal PR (all lanes run), a docs-only PR
      (lanes skipped, gate green), a PR with a deliberately failing lane (gate
      red), and a PR where the planner job fails (gate red, or the planner
      falls back to everything).
- [ ] Fork PRs: secrets and tokens differ. The planner must work without them
      or fall back to the full set.

## Other CI systems (short notes; check the docs)

- **GitLab:** `rules: changes:` evaluates against the push or MR diff. On a new
  branch or a missing diff it can evaluate as "changed" (runs) or not, depending
  on `compare_to`. Set `compare_to: refs/heads/main` explicitly. "Pipelines must
  succeed" counts skipped jobs as success. Use `needs:` with an aggregation job
  as above.
- **CircleCI:** dynamic config (`setup: true` + path-filtering orb) decides
  workflows. Required checks are per workflow or job name, with the same
  "never reported → blocked" problem.
- **Buildkite:** `if_changed` or dynamic pipeline upload. The GitHub status
  comes from the pipeline, so keep a single required pipeline status and do the
  gating inside it.
