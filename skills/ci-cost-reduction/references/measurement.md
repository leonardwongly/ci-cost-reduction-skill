# Measuring CI: metrics, pitfalls, other providers

## Metrics and what they tell you

| Metric | Definition (as `ci_baseline.py` computes it) | Why it matters |
|---|---|---|
| Job minutes | Σ (completed − started) over jobs that ran | Raw compute |
| Billable minutes | Σ ceil(job minutes) per job (GitHub-hosted rounds each job up to a whole minute) | What's charged. Many tiny jobs inflate it |
| Rounding overhead | (billable − job) / job | High → merge tiny jobs |
| Est. cost | billable × rate for the runner class (`--rates`) | Only as good as the rates file |
| Per-PR / per-run / per-active-day | Totals ÷ volume | **The before/after comparison.** Totals move with traffic |
| PR wall-clock (attempt 1) | First job created → last job completed, PR events, first attempt | Feedback time per workflow run. For several workflows per PR, the slowest required one is what developers wait for |
| Queue time | Job created → job started | Runner capacity. Cheaper or self-hosted runners often add queueing |
| Parallelism | Σ job minutes ÷ Σ wall minutes | High with long wall-clock → critical-path problem, not volume |
| Cancelled / failed / rerun minutes | Billable minutes by job conclusion or attempt > 1 | Waste you can remove |
| Flake signal | Workflow+SHA with a failed attempt later followed by a green one; per job, failed→passed on the same SHA | Flakes cost reruns and trust |
| Setup-like step share | Step time matching checkout/setup/install/cache/download ÷ all step time | Environment reuse opportunity (the regex is a heuristic. Read the top-steps list) |
| Jobs for 80% of minutes | Fewest job types that make up 80% of billable minutes | Where to focus |

## Pitfalls

- **Window choice.** At least two full weeks (14 days), so weekday effects cancel out; the
  same length before and after; avoid release weeks or holidays, or note them.
  The runs API filters on *created* time. Runs straddling the window edge are
  attributed to their creation day.
- **Volume drift.** OpenClaw's commit volume fell about 44% over its comparison
  period. Always report normalised rows next to totals.
- **Sampling.** `--max-runs` takes the *earliest* runs in the window, which is
  biased. Prefer `--sample N` (every Nth run) for an even spread, and record it.
- **Reruns.** `fetch` uses `filter=all`, so jobs from every attempt are included
  (reruns cost money). Feedback-time stats use attempt 1.
- **Skipped jobs** cost 0 and are excluded from duration stats.
- **Fork PRs** often show `pull_requests: []` in the API. Distinct-PR counts
  can undercount on fork-heavy repos. Use `pr_runs` as a cross-check.
- **Self-hosted runners** aren't free: amortise the machines, operations time,
  and idle capacity. Set a per-minute rate, or report minutes only.
- **Third-party runners** (Blacksmith, RunsOn, Namespace, BuildJet, etc.) show
  up as runner labels. Map them in the rates file at the vendor's price.
- **Billing sources.** If the org has billing access, cross-check against the
  provider's usage report (GitHub: org Settings → Billing → Usage, or the
  billing usage API). The script estimates. The invoice is the truth.
- **Noise.** Wall-clock varies run to run by 10–30% easily. For a single-change
  claim, either use enough runs (about 30 or more) or run the same commit
  several times on both versions.

## Normalised JSONL schema (for other providers)

One JSON object per line, one per job attempt. Required fields are in bold:

```json
{
  "provider": "gitlab",
  "run_id": 123,                 // pipeline / build / workflow-run id (bold)
  "run_attempt": 1,              // 1 for first attempt; retries > 1 (bold)
  "workflow": "ci",              // pipeline / workflow name (bold)
  "event": "pull_request",       // use pull_request for MR/PR-triggered, push, schedule, merge_group (bold)
  "branch": "feature/x",
  "head_sha": "abc123",          // (bold) used for flake detection
  "pull_requests": [42],
  "run_created_at": "2026-11-01T10:00:00Z",
  "run_conclusion": "success",
  "job_id": 987,                 // (bold)
  "job_name": "test (3/8)",      // (bold) as reported to the VCS
  "job_created_at": "2026-11-01T10:00:05Z",   // (bold) queued time
  "job_started_at": "2026-11-01T10:00:40Z",   // (bold)
  "job_completed_at": "2026-11-01T10:07:12Z", // (bold)
  "job_conclusion": "success",   // success|failure|cancelled|skipped (bold)
  "labels": ["saas-linux-medium-amd64"],      // runner class identifiers used by --rates
  "runner_group": null,
  "steps": [["checkout", "success", 4.1], ["npm ci", "success", 61.0]]  // optional [name, conclusion, seconds]
}
```

Where to get the data:
- **GitLab:** `GET /projects/:id/pipelines?updated_after=…` then
  `GET /projects/:id/pipelines/:pid/jobs?include_retried=true`. Fields: `queued_duration`,
  `started_at`, `finished_at`, `status`, `tag_list`/runner description,
  `retried`. Map `merge_request_event` → `pull_request`.
- **CircleCI:** Insights API (`/insights/{project-slug}/workflows/{name}/jobs`) for aggregates;
  `/pipeline` → `/workflow/{id}/job` → `/project/{slug}/job/{n}` for per-job timing and
  `resource_class`.
- **Buildkite:** `GET /organizations/{org}/pipelines/{p}/builds?created_from=…` includes jobs with
  `scheduled_at`, `runnable_at`, `started_at`, `finished_at`, `agent_query_rules`.
- **Jenkins:** `/job/<name>/api/json?tree=builds[number,result,timestamp,duration,actions[...]]`;
  use the queue-time action if installed. Per-stage timing comes from the Pipeline
  Stage View or `wfapi/describe`.
- **No API access:** run the pipeline steps locally with `time`, or ask the user
  to export a usage CSV. Mark the baseline as an approximation.

## Local, controlled measurement

To isolate one change from traffic noise:
1. Pick 2–3 representative commits (a small change, a medium change and a
   wide one).
2. Run the old and the new pipeline on each, 3 or more times each (manual
   dispatch on a branch, or `act`/local scripts for single jobs).
3. Report each run's figures and the median. Don't report only the best run.
