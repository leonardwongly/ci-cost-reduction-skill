#!/usr/bin/env python3
"""Measure a CI baseline and compare before/after.

Subcommands
  fetch    GitHub Actions runs+jobs in a date window -> normalized JSONL (one record per job attempt)
  analyze  JSONL -> summary JSON + Markdown (cost, minutes, feedback time, queue, waste, flake signal)
  compare  two summary JSONs -> delta table

Standard library only. Other CI providers: emit the same JSONL schema (see
references/measurement.md) and use analyze/compare unchanged.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SETUP_STEP_RE = re.compile(
    r"(set up job|checkout|setup[- ]|set up |install|cache|restore|download|"
    r"docker pull|pull image|initialize containers|post |complete job)",
    re.I,
)

# --------------------------------------------------------------------------- utils


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def secs(a, b):
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def pct(values, p):
    """Nearest-rank percentile; None for empty."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, math.ceil(p / 100 * len(vals)) - 1))
    return vals[k]


def r(x, nd=1):
    return None if x is None else round(x, nd)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- fetch


class GitHub:
    def __init__(self, api_url, token):
        self.api = api_url.rstrip("/")
        self.token = token
        self.requests = 0

    def get(self, path, params=None):
        url = path if path.startswith("http") else f"{self.api}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-baseline-script",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(6):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self.requests += 1
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                self.requests += 1
                remaining = e.headers.get("X-RateLimit-Remaining")
                if e.code in (403, 429) and (remaining == "0" or e.headers.get("Retry-After")):
                    reset = int(e.headers.get("X-RateLimit-Reset", "0") or 0)
                    wait = int(e.headers.get("Retry-After") or max(1, reset - int(time.time())))
                    if wait > 900:
                        raise SystemExit(
                            f"Rate limited; resets in {wait}s. Re-run later, use --max-runs/--sample, or authenticate."
                        )
                    log(f"rate limited, sleeping {wait}s")
                    time.sleep(wait + 1)
                    continue
                if e.code >= 500 and attempt < 5:
                    time.sleep(2**attempt)
                    continue
                body = e.read().decode(errors="replace")[:300]
                raise SystemExit(f"GitHub API {e.code} for {url}: {body}")
            except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, ValueError) as e:
                if attempt < 5:
                    time.sleep(2**attempt)
                    continue
                raise SystemExit(f"Network error for {url}: {e}")
        raise SystemExit(f"Giving up on {url}")


def get_token(no_auth):
    if no_auth:
        return None
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or None
    except Exception:
        return None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def list_runs(gh, repo, start, end, filters):
    """Yield completed runs created in [start, end). Splits windows to stay under the 1000-result cap."""
    q = dict(filters)
    q.update({"created": f"{iso(start)}..{iso(end - timedelta(seconds=1))}", "per_page": 100, "status": "completed"})
    first = gh.get(f"/repos/{repo}/actions/runs", {**q, "page": 1})
    total = first.get("total_count", 0)
    if total > 1000 and (end - start) > timedelta(minutes=30):
        mid = start + (end - start) / 2
        yield from list_runs(gh, repo, start, mid, filters)
        yield from list_runs(gh, repo, mid, end, filters)
        return
    if total > 1000:
        log(f"warning: {total} runs in {iso(start)}..{iso(end)}; only first 1000 retrievable")
    runs = first.get("workflow_runs", [])
    yield from runs
    page = 2
    while len(runs) == 100 and (page - 1) * 100 < min(total, 1000):
        data = gh.get(f"/repos/{repo}/actions/runs", {**q, "page": page})
        runs = data.get("workflow_runs", [])
        yield from runs
        page += 1


def list_jobs(gh, repo, run_id):
    jobs, page = [], 1
    while True:
        data = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs", {"filter": "all", "per_page": 100, "page": page})
        batch = data.get("jobs", [])
        jobs.extend(batch)
        if len(batch) < 100 or len(jobs) >= data.get("total_count", 0):
            return jobs
        page += 1


def normalize(run, job, repo):
    steps = []
    for s in job.get("steps") or []:
        d = secs(parse_ts(s.get("started_at")), parse_ts(s.get("completed_at")))
        steps.append([s.get("name"), s.get("conclusion"), r(d, 1)])
    # The API attaches unrelated (often fork) PRs to non-PR runs; keep only PRs into this repo on PR events.
    prs = []
    if (run.get("event") or "").startswith("pull_request"):
        for p in run.get("pull_requests") or []:
            base_url = ((p.get("base") or {}).get("repo") or {}).get("url", "")
            if p.get("number") and base_url.lower().endswith("/repos/" + repo.lower()):
                prs.append(p["number"])
    return {
        "provider": "github",
        "run_id": run["id"],
        "run_attempt": job.get("run_attempt") or run.get("run_attempt") or 1,
        "workflow": run.get("name") or run.get("path"),
        "workflow_path": run.get("path"),
        "event": run.get("event"),
        "branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "pull_requests": prs,
        "run_created_at": run.get("created_at"),
        "run_conclusion": run.get("conclusion"),
        "job_id": job["id"],
        "job_name": job.get("name"),
        "job_created_at": job.get("created_at"),
        "job_started_at": job.get("started_at"),
        "job_completed_at": job.get("completed_at"),
        "job_conclusion": job.get("conclusion"),
        "labels": job.get("labels") or [],
        "runner_group": job.get("runner_group_name"),
        "steps": steps,
    }


def cmd_fetch(a):
    gh = GitHub(a.api_url, get_token(a.no_auth))
    start = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc)
    if end <= start:
        raise SystemExit("--until must be after --since")
    filters = {}
    if a.event:
        filters["event"] = a.event
    if a.branch:
        filters["branch"] = a.branch
    wf = set(x.strip() for x in a.workflow.split(",")) if a.workflow else None

    n_runs = n_jobs = seen = 0
    with open(a.out, "w") as fh:
        day = start
        while day < end:
            nxt = min(day + timedelta(days=1), end)
            for run in list_runs(gh, a.repo, day, nxt, filters):
                if wf and run.get("name") not in wf and os.path.basename(run.get("path", "")) not in wf:
                    continue
                seen += 1
                if a.sample > 1 and (seen - 1) % a.sample:
                    continue
                if a.max_runs and n_runs >= a.max_runs:
                    break
                for job in list_jobs(gh, a.repo, run["id"]):
                    fh.write(json.dumps(normalize(run, job, a.repo)) + "\n")
                    n_jobs += 1
                n_runs += 1
                if n_runs % 50 == 0:
                    log(f"{n_runs} runs, {n_jobs} jobs, {gh.requests} API requests")
            if a.max_runs and n_runs >= a.max_runs:
                log(f"stopped at --max-runs {a.max_runs}; data is a partial sample")
                break
            day = nxt
    meta = {
        "repo": a.repo, "since": a.since, "until": a.until, "event": a.event, "branch": a.branch,
        "workflow": a.workflow, "runs_seen": seen, "runs_fetched": n_runs, "jobs": n_jobs,
        "sample_every": a.sample, "max_runs": a.max_runs, "api_requests": gh.requests,
        "fetched_at": iso(datetime.now(timezone.utc)),
    }
    with open(a.out + ".meta.json", "w") as fh:
        json.dump(meta, fh, indent=2)
    log(json.dumps(meta))


# --------------------------------------------------------------------------- analyze


def load_rates(path):
    if not path:
        return None
    with open(path) as fh:
        cfg = json.load(fh)
    rules = [(re.compile(x["match"], re.I), float(x["per_minute"]), x.get("name") or x["match"]) for x in cfg["rates"]]
    return {"rules": rules, "round_up": cfg.get("round_up_per_job", True), "currency": cfg.get("currency", "USD")}


def runner_class(labels, rates):
    key = ",".join(labels) or "(none)"
    if rates:
        for rx, rate, name in rates["rules"]:
            if rx.search(key):
                return name, rate
    return key, None


def base_job_name(name):
    # "test (ubuntu-latest, 20)" -> "test"; "shard 3/8" -> "shard"
    n = re.sub(r"\s*\(.*\)\s*$", "", name or "")
    n = re.sub(r"\s*\b\d+\s*/\s*\d+\b", "", n)
    return n.strip() or (name or "?")


def stats(values):
    vals = [v for v in values if v is not None]
    return {"n": len(vals), "p50": r(pct(vals, 50)), "p90": r(pct(vals, 90)), "max": r(max(vals) if vals else None)}


def cmd_analyze(a):
    rates = load_rates(a.rates)
    required = [x.strip() for x in a.required.split(",")] if a.required else []
    recs = []
    with open(a.jsonl) as fh:
        for line in fh:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    if not recs:
        raise SystemExit("no records")
    meta = {}
    if os.path.exists(a.jsonl + ".meta.json"):
        meta = json.load(open(a.jsonl + ".meta.json"))

    attempts = defaultdict(list)  # (run_id, attempt) -> recs
    for x in recs:
        x["_c"] = parse_ts(x.get("job_created_at"))
        x["_s"] = parse_ts(x.get("job_started_at"))
        x["_e"] = parse_ts(x.get("job_completed_at"))
        ran = x.get("job_conclusion") not in ("skipped", None) and x["_s"] and x["_e"]
        x["_min"] = (secs(x["_s"], x["_e"]) / 60) if ran else 0.0
        x["_bill"] = (math.ceil(x["_min"]) if (rates is None or rates["round_up"]) else x["_min"]) if ran else 0
        x["_queue"] = secs(x["_c"], x["_s"]) if ran else None
        x["_class"], x["_rate"] = runner_class(x.get("labels") or [], rates)
        x["_cost"] = (x["_bill"] * x["_rate"]) if x["_rate"] is not None else None
        attempts[(x["run_id"], x["run_attempt"])].append(x)

    all_created = [x["_c"] for x in recs if x["_c"]]
    span_days = max(1e-9, secs(min(all_created), max(all_created)) / 86400) if all_created else None
    active_days = len({x["_c"].date() for x in recs if x["_c"]})

    def total(key, items):
        return sum(x[key] for x in items if x[key] is not None)

    unpriced = sum(x["_bill"] for x in recs if x["_rate"] is None and x["_bill"])
    pr_numbers = {p for x in recs for p in (x.get("pull_requests") or [])}
    pr_runs = {x["run_id"] for x in recs if x.get("event") in ("pull_request", "pull_request_target")}

    # attempt-level wall clock (first job created -> last job completed)
    att_rows = []
    for (rid, att), items in attempts.items():
        cs = [x["_c"] for x in items if x["_c"]]
        es = [x["_e"] for x in items if x["_e"]]
        wall = secs(min(cs), max(es)) if cs and es else None
        crit = max((x["_min"] for x in items), default=0)
        att_rows.append({
            "run_id": rid, "attempt": att, "workflow": items[0].get("workflow"), "event": items[0].get("event"),
            "sha": items[0].get("head_sha"), "wall_min": wall / 60 if wall is not None else None,
            "job_min": sum(x["_min"] for x in items), "longest_job_min": crit,
            "failed": any(x.get("job_conclusion") == "failure" for x in items),
            "cancelled": any(x.get("job_conclusion") == "cancelled" for x in items),
            "ok": all(x.get("job_conclusion") in ("success", "skipped", "neutral") for x in items),
            "created": min(cs) if cs else None,
        })

    pr_first = [x for x in att_rows if x["event"] in ("pull_request", "pull_request_target") and x["attempt"] == 1]

    # flake signal: same workflow+sha, a failed attempt/run followed later by a successful one
    by_sha = defaultdict(list)
    for x in att_rows:
        by_sha[(x["workflow"], x["sha"])].append(x)
    flaky_pairs = 0
    for rows in by_sha.values():
        rows.sort(key=lambda z: z["created"] or datetime.min.replace(tzinfo=timezone.utc))
        seen_fail = False
        for z in rows:
            if z["failed"]:
                seen_fail = True
            elif z["ok"] and seen_fail:
                flaky_pairs += 1
                break
    job_flake = defaultdict(lambda: [0, 0])  # base name -> [failed-then-passed shas, shas with >1 attempt]
    by_job_sha = defaultdict(list)
    for x in recs:
        by_job_sha[(x.get("workflow"), x.get("job_name"), x.get("head_sha"))].append(x)
    for (wfn, jn, sha), items in by_job_sha.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda z: z["_c"] or datetime.min.replace(tzinfo=timezone.utc))
        k = f"{wfn} / {base_job_name(jn)}"
        job_flake[k][1] += 1
        concl = [z.get("job_conclusion") for z in items]
        if "failure" in concl and concl[-1] == "success" and concl.index("failure") < len(concl) - 1:
            job_flake[k][0] += 1

    # per workflow
    wf = defaultdict(lambda: {"attempts": 0, "job_min": 0.0, "bill": 0, "cost": 0.0, "walls": [], "fail": 0, "cancel": 0})
    for x in att_rows:
        w = wf[x["workflow"]]
        w["attempts"] += 1
        w["walls"].append(x["wall_min"])
        w["fail"] += x["failed"]
        w["cancel"] += x["cancelled"]
    for x in recs:
        w = wf[x.get("workflow")]
        w["job_min"] += x["_min"]
        w["bill"] += x["_bill"]
        w["cost"] += x["_cost"] or 0
    total_bill = total("_bill", recs) or 1

    workflows = sorted(
        ({"workflow": k, "attempts": v["attempts"], "job_min": r(v["job_min"]), "billable_min": r(v["bill"], 0),
          "share_pct": r(100 * v["bill"] / total_bill), "est_cost": r(v["cost"], 2) if rates else None,
          "wall_min": stats(v["walls"]), "fail_rate_pct": r(100 * v["fail"] / max(1, v["attempts"])),
          "cancel_rate_pct": r(100 * v["cancel"] / max(1, v["attempts"]))} for k, v in wf.items()),
        key=lambda z: -z["billable_min"],
    )

    # per job (base name)
    jb = defaultdict(lambda: {"n": 0, "mins": [], "bill": 0, "cost": 0.0, "fail": 0, "queue": []})
    for x in recs:
        if x["_min"] == 0 and x.get("job_conclusion") == "skipped":
            continue
        j = jb[f"{x.get('workflow')} / {base_job_name(x.get('job_name'))}"]
        j["n"] += 1
        j["mins"].append(x["_min"])
        j["bill"] += x["_bill"]
        j["cost"] += x["_cost"] or 0
        j["fail"] += x.get("job_conclusion") == "failure"
        j["queue"].append(x["_queue"])
    jobs = sorted(
        ({"job": k, "runs": v["n"], "billable_min": r(v["bill"], 0), "share_pct": r(100 * v["bill"] / total_bill),
          "est_cost": r(v["cost"], 2) if rates else None, "duration_min": stats(v["mins"]),
          "queue_s": stats(v["queue"]), "fail_rate_pct": r(100 * v["fail"] / max(1, v["n"])),
          "failed_then_passed": job_flake.get(k, [0, 0])[0]} for k, v in jb.items()),
        key=lambda z: -z["billable_min"],
    )
    cum, top_share_jobs = 0.0, 0
    for j in jobs:
        cum += j["share_pct"] or 0
        top_share_jobs += 1
        if cum >= 80:
            break

    # runners
    # Keyed by raw labels so a wrong rate match is visible next to the label it priced.
    rc = defaultdict(lambda: {"jobs": 0, "min": 0.0, "bill": 0, "cost": 0.0, "rate": None, "rule": None})
    for x in recs:
        c = rc[",".join(x.get("labels") or []) or "(no runner: skipped/never started)"]
        c["jobs"] += 1
        c["min"] += x["_min"]
        c["bill"] += x["_bill"]
        c["cost"] += x["_cost"] or 0
        c["rate"] = x["_rate"]
        c["rule"] = x["_class"] if x["_rate"] is not None else None
    runners = sorted(
        ({"labels": k, "priced_as": v["rule"], "jobs": v["jobs"], "job_min": r(v["min"]),
          "billable_min": r(v["bill"], 0), "per_minute": v["rate"],
          "est_cost": r(v["cost"], 2) if v["rate"] is not None else None}
         for k, v in rc.items()),
        key=lambda z: -z["billable_min"],
    )

    # steps
    st = defaultdict(float)
    setup_s = all_step_s = 0.0
    for x in recs:
        for name, _c, d in x.get("steps") or []:
            if d is None:
                continue
            st[name] += d
            all_step_s += d
            if SETUP_STEP_RE.search(name or ""):
                setup_s += d
    top_steps = [{"step": k, "total_min": r(v / 60), "share_pct": r(100 * v / all_step_s) if all_step_s else None}
                 for k, v in sorted(st.items(), key=lambda kv: -kv[1])[:15]]

    # waste
    def mins_where(pred):
        return r(sum(x["_bill"] for x in recs if pred(x)), 0)

    n_pr_runs = len(pr_runs)
    tot_bill = total("_bill", recs)
    tot_cost = total("_cost", recs) if rates else None
    queues = [x["_queue"] for x in recs]
    summary = {
        "meta": meta,
        "window": {
            "first_job": iso(min(all_created)) if all_created else None,
            "last_job": iso(max(all_created)) if all_created else None,
            "span_days": r(span_days, 2), "active_days": active_days,
        },
        "volume": {
            "runs": len({x["run_id"] for x in recs}), "attempts": len(att_rows), "jobs": len(recs),
            "pr_runs": n_pr_runs, "distinct_prs": len(pr_numbers),
            "rerun_attempts": sum(1 for x in att_rows if x["attempt"] > 1),
        },
        "cost": {
            "job_min": r(total("_min", recs)), "billable_min": r(tot_bill, 0),
            "rounding_overhead_pct": r(100 * (tot_bill - total("_min", recs)) / max(1e-9, total("_min", recs))),
            "est_cost": r(tot_cost, 2) if rates else None, "currency": rates["currency"] if rates else None,
            "unpriced_billable_min": r(unpriced, 0) if rates else None,
            "billable_min_per_run": r(tot_bill / max(1, len(att_rows))),
            "billable_min_per_pr": r(tot_bill / len(pr_numbers)) if pr_numbers else None,
            "est_cost_per_pr": r(tot_cost / len(pr_numbers), 3) if (rates and pr_numbers) else None,
            "billable_min_per_active_day": r(tot_bill / max(1, active_days)),
            "jobs_for_80pct_of_minutes": top_share_jobs,
        },
        "feedback": {
            "pr_attempt1_wall_min": stats([x["wall_min"] for x in pr_first]),
            "pr_attempt1_wall_min_success_only": stats([x["wall_min"] for x in pr_first if x["ok"]]),
            "all_attempt_wall_min": stats([x["wall_min"] for x in att_rows]),
            "job_queue_s": stats(queues),
            "parallelism": r(sum(x["job_min"] for x in att_rows) / max(1e-9, sum(x["wall_min"] or 0 for x in att_rows)), 2),
        },
        "waste": {
            "cancelled_job_billable_min": mins_where(lambda x: x.get("job_conclusion") == "cancelled"),
            "failed_job_billable_min": mins_where(lambda x: x.get("job_conclusion") == "failure"),
            "rerun_attempt_billable_min": mins_where(lambda x: (x.get("run_attempt") or 1) > 1),
            "setup_like_step_share_pct": r(100 * setup_s / all_step_s) if all_step_s else None,
            "flake_signal_sha_pairs": flaky_pairs,
            "attempt_fail_rate_pct": r(100 * sum(x["failed"] for x in att_rows) / max(1, len(att_rows))),
            "attempt_cancel_rate_pct": r(100 * sum(x["cancelled"] for x in att_rows) / max(1, len(att_rows))),
        },
        "workflows": workflows,
        "jobs": jobs[: a.top],
        "runners": runners,
        "top_steps": top_steps,
    }
    if required:
        names = {x.get("job_name") for x in recs}
        summary["required_checks_seen"] = {n: (n in names) for n in required}

    with open(a.out, "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    md = render_md(summary, rates is not None)
    if a.md:
        with open(a.md, "w") as fh:
            fh.write(md)
    print(md)


def fmt(v, suf=""):
    return "—" if v is None else f"{v}{suf}"


def render_md(s, priced):
    L = []
    w, v, c, f, wa = s["window"], s["volume"], s["cost"], s["feedback"], s["waste"]
    m = s.get("meta") or {}
    L.append(f"# CI baseline: {m.get('repo', '')}".rstrip())
    L.append("")
    L.append(f"Window: {w['first_job']} → {w['last_job']} ({w['span_days']} days, {w['active_days']} active). "
             f"Runs {v['runs']}, attempts {v['attempts']}, jobs {v['jobs']}, PR runs {v['pr_runs']}, distinct PRs {v['distinct_prs']}.")
    if m.get("max_runs") or (m.get("sample_every") or 1) > 1:
        L.append(f"**Sampled:** max_runs={m.get('max_runs')}, every {m.get('sample_every')}th run; "
                 f"{m.get('runs_fetched')} of {m.get('runs_seen')} runs seen.")
    L.append("")
    L.append("## Cost")
    L.append(f"- Job minutes {c['job_min']}; billable minutes {c['billable_min']} (per-job rounding adds {fmt(c['rounding_overhead_pct'], '%')})")
    if priced:
        L.append(f"- Estimated cost {fmt(c['est_cost'])} {c['currency']} (unpriced billable minutes: {c['unpriced_billable_min']})")
        L.append(f"- Estimated cost per PR {fmt(c['est_cost_per_pr'])}")
    L.append(f"- Billable min per run attempt {c['billable_min_per_run']}, per PR {fmt(c['billable_min_per_pr'])}, per active day {c['billable_min_per_active_day']}")
    L.append(f"- {c['jobs_for_80pct_of_minutes']} job types account for 80% of billable minutes")
    L.append("")
    L.append("## Feedback time")
    p, ps, q = f["pr_attempt1_wall_min"], f["pr_attempt1_wall_min_success_only"], f["job_queue_s"]
    L.append(f"- PR run wall-clock (first attempt): p50 {fmt(p['p50'])} min, p90 {fmt(p['p90'])} min, n={p['n']}")
    L.append(f"- …green runs only: p50 {fmt(ps['p50'])} min, p90 {fmt(ps['p90'])} min")
    L.append(f"- Job queue time: p50 {fmt(q['p50'])} s, p90 {fmt(q['p90'])} s")
    L.append(f"- Parallelism (job-min / wall-min): {f['parallelism']}")
    L.append("")
    L.append("## Waste")
    L.append(f"- Cancelled jobs: {wa['cancelled_job_billable_min']} billable min; failed jobs: {wa['failed_job_billable_min']}; rerun attempts: {wa['rerun_attempt_billable_min']}")
    L.append(f"- Attempt failure rate {wa['attempt_fail_rate_pct']}%, cancel rate {wa['attempt_cancel_rate_pct']}%")
    L.append(f"- Flake signal: {wa['flake_signal_sha_pairs']} workflow+SHA pairs failed then passed")
    L.append(f"- Setup-like steps: {fmt(wa['setup_like_step_share_pct'], '%')} of step time")
    L.append("")
    L.append("## Workflows")
    L.append("| Workflow | Attempts | Billable min | Share | Est. cost | Wall p50/p90 | Fail % | Cancel % |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for x in s["workflows"]:
        L.append(f"| {x['workflow']} | {x['attempts']} | {x['billable_min']} | {x['share_pct']}% | {fmt(x['est_cost'])} | "
                 f"{fmt(x['wall_min']['p50'])}/{fmt(x['wall_min']['p90'])} | {x['fail_rate_pct']} | {x['cancel_rate_pct']} |")
    L.append("")
    L.append("## Top jobs by billable minutes")
    L.append("| Job | Runs | Billable min | Share | Dur p50/p90 min | Queue p90 s | Fail % | Failed→passed |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for x in s["jobs"]:
        d = x["duration_min"]
        L.append(f"| {x['job']} | {x['runs']} | {x['billable_min']} | {x['share_pct']}% | {fmt(d['p50'])}/{fmt(d['p90'])} | "
                 f"{fmt(x['queue_s']['p90'])} | {x['fail_rate_pct']} | {x['failed_then_passed']} |")
    L.append("")
    L.append("## Runner labels")
    if priced:
        L.append("Check that each label set is priced by the right rule. Custom labels often fall through to a generic rule.")
        L.append("")
    L.append("| Labels | Priced as | Jobs | Billable min | Rate/min | Est. cost |")
    L.append("|---|---|---:|---:|---:|---:|")
    for x in s["runners"]:
        L.append(f"| {x['labels']} | {fmt(x['priced_as'])} | {x['jobs']} | {x['billable_min']} | {fmt(x['per_minute'])} | {fmt(x['est_cost'])} |")
    L.append("")
    L.append("## Top steps by time")
    for x in s["top_steps"]:
        L.append(f"- {x['step']}: {x['total_min']} min ({fmt(x['share_pct'], '%')})")
    if s.get("required_checks_seen"):
        L.append("")
        L.append("## Required checks seen in data")
        for k, ok in s["required_checks_seen"].items():
            L.append(f"- {'✅' if ok else '❌ not seen'} {k}")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- compare

COMPARE_KEYS = [
    ("volume", "attempts", "Run attempts", None),
    ("volume", "distinct_prs", "Distinct PRs", None),
    ("cost", "billable_min", "Billable minutes", "lower"),
    ("cost", "est_cost", "Estimated cost", "lower"),
    ("cost", "billable_min_per_run", "Billable min / run attempt", "lower"),
    ("cost", "billable_min_per_pr", "Billable min / PR", "lower"),
    ("cost", "est_cost_per_pr", "Est. cost / PR", "lower"),
    ("cost", "billable_min_per_active_day", "Billable min / active day", "lower"),
    ("feedback", "pr_attempt1_wall_min.p50", "PR wall-clock p50 (min)", "lower"),
    ("feedback", "pr_attempt1_wall_min.p90", "PR wall-clock p90 (min)", "lower"),
    ("feedback", "pr_attempt1_wall_min_success_only.p50", "Green PR wall-clock p50 (min)", "lower"),
    ("feedback", "job_queue_s.p90", "Job queue p90 (s)", "lower"),
    ("waste", "cancelled_job_billable_min", "Cancelled-job minutes", "lower"),
    ("waste", "rerun_attempt_billable_min", "Rerun-attempt minutes", "lower"),
    ("waste", "attempt_fail_rate_pct", "Attempt failure rate %", None),
    ("waste", "flake_signal_sha_pairs", "Failed→passed SHA pairs", "lower"),
    ("waste", "setup_like_step_share_pct", "Setup-like step share %", "lower"),
]


def dig(d, sect, key):
    cur = d.get(sect, {})
    for part in key.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def cmd_compare(a):
    b, af = json.load(open(a.before)), json.load(open(a.after))
    L = ["# CI before/after", ""]
    bw, aw = b["window"], af["window"]
    L.append(f"Before: {bw['first_job']} → {bw['last_job']} ({bw['span_days']} d) · After: {aw['first_job']} → {aw['last_job']} ({aw['span_days']} d)")
    warn = []
    if bw["span_days"] and aw["span_days"] and abs(bw["span_days"] - aw["span_days"]) / max(bw["span_days"], aw["span_days"]) > 0.2:
        warn.append("Window lengths differ by >20%: compare normalized rows (per run / per PR / per day), not totals.")
    ba, aa = b["volume"]["attempts"], af["volume"]["attempts"]
    if ba and aa and abs(ba - aa) / max(ba, aa) > 0.25:
        warn.append(f"Run volume changed {ba} → {aa}: part of any total change is volume, not efficiency.")
    if af["volume"]["attempts"] < 30:
        warn.append("Fewer than 30 run attempts after the change: treat percentiles as indicative only.")
    for x in warn:
        L.append(f"> ⚠️ {x}")
    L.append("")
    L.append("| Metric | Before | After | Δ | Δ % | |")
    L.append("|---|---:|---:|---:|---:|---|")
    for sect, key, label, better in COMPARE_KEYS:
        x, y = dig(b, sect, key), dig(af, sect, key)
        if x is None and y is None:
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            d = y - x
            dp = (100 * d / x) if x else None
            mark = ""
            if better and d:
                mark = "✅" if (d < 0) == (better == "lower") else "⚠️"
            L.append(f"| {label} | {r(x, 2)} | {r(y, 2)} | {r(d, 2)} | {fmt(r(dp, 1), '%')} | {mark} |")
        else:
            L.append(f"| {label} | {fmt(x)} | {fmt(y)} | | | |")
    md = "\n".join(L) + "\n"
    if a.md:
        with open(a.md, "w") as fh:
            fh.write(md)
    print(md)


# --------------------------------------------------------------------------- main


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch GitHub Actions runs+jobs to JSONL")
    f.add_argument("--repo", required=True, help="OWNER/REPO")
    f.add_argument("--since", required=True, help="YYYY-MM-DD (inclusive, UTC)")
    f.add_argument("--until", required=True, help="YYYY-MM-DD (exclusive, UTC)")
    f.add_argument("--out", required=True)
    f.add_argument("--event", help="e.g. pull_request, push, merge_group")
    f.add_argument("--branch")
    f.add_argument("--workflow", help="comma-separated workflow names or file names")
    f.add_argument("--max-runs", type=int, default=0)
    f.add_argument("--sample", type=int, default=1, help="keep every Nth run")
    f.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    f.add_argument("--no-auth", action="store_true", help="unauthenticated (public repos, 60 req/h)")
    f.set_defaults(fn=cmd_fetch)

    an = sub.add_parser("analyze", help="summarize JSONL")
    an.add_argument("jsonl")
    an.add_argument("--rates", help="rates JSON (see assets/rates.example.json)")
    an.add_argument("--required", help="comma-separated required check (job) names to confirm presence")
    an.add_argument("--out", required=True)
    an.add_argument("--md")
    an.add_argument("--top", type=int, default=25)
    an.set_defaults(fn=cmd_analyze)

    c = sub.add_parser("compare", help="compare two analyze outputs")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("--md")
    c.set_defaults(fn=cmd_compare)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
