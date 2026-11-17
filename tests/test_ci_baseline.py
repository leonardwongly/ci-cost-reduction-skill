"""Tests for skills/ci-cost-reduction/scripts/ci_baseline.py (stdlib unittest).

Run: python3 -m unittest discover -s tests
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "ci-cost-reduction"
SCRIPT = SKILL / "scripts" / "ci_baseline.py"
RATES = SKILL / "assets" / "rates.example.json"

T = "2026-11-01T10:%s:00Z"


def job(run, att, sha, name, c, s, e, concl, labels=("ubuntu-latest",), ev="pull_request", prs=(7,)):
    return {
        "provider": "test", "run_id": run, "run_attempt": att, "workflow": "CI", "event": ev,
        "head_sha": sha, "pull_requests": list(prs), "job_id": f"{run}-{att}-{name}", "job_name": name,
        "job_created_at": c, "job_started_at": s, "job_completed_at": e, "job_conclusion": concl,
        "labels": list(labels),
        "steps": [["Run actions/checkout@v4", "success", 10], ["npm test", "success", 50]],
    }


FIXTURE = [
    job(1, 1, "a", "test (1/2)", T % "00", T % "01", "2026-11-01T10:01:30Z", "success"),    # 0.5 min -> 1 billable
    job(1, 1, "a", "test (2/2)", T % "00", T % "01", "2026-11-01T10:02:30Z", "success"),    # 1.5 -> 2
    job(2, 1, "b", "test (1/2)", T % "10", T % "10", "2026-11-01T10:13:00Z", "failure"),    # 3 -> 3
    job(2, 1, "b", "test (2/2)", T % "10", T % "10", "2026-11-01T10:11:00Z", "cancelled"),  # 1 -> 1
    job(2, 2, "b", "test (1/2)", T % "20", T % "20", "2026-11-01T10:23:00Z", "success"),    # rerun 3 -> 3
    job(2, 2, "b", "test (2/2)", T % "20", T % "20", "2026-11-01T10:22:00Z", "success"),    # rerun 2 -> 2
    job(3, 1, "c", "docs", T % "30", None, None, "skipped", labels=()),
    job(4, 1, "d", "build", "2026-11-02T09:00:00Z", "2026-11-02T09:00:00Z", "2026-11-02T09:10:00Z",
        "success", labels=("macos-15",), ev="push", prs=()),                                  # 10 -> 10
]


def run(*args):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, check=True)


class AnalyzeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.jsonl = d / "fx.jsonl"
        cls.jsonl.write_text("\n".join(json.dumps(r) for r in FIXTURE) + "\n")
        cls.out = d / "fx.json"
        run("analyze", str(cls.jsonl), "--rates", str(RATES), "--required", "test (1/2),lint",
            "--out", str(cls.out), "--md", str(d / "fx.md"))
        cls.s = json.loads(cls.out.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_minutes_and_per_job_rounding(self):
        self.assertAlmostEqual(self.s["cost"]["job_min"], 21.0)
        self.assertEqual(self.s["cost"]["billable_min"], 22)

    def test_cost_uses_runner_rates(self):
        self.assertAlmostEqual(self.s["cost"]["est_cost"], round(12 * 0.008 + 10 * 0.08, 2))

    def test_waste_buckets(self):
        w = self.s["waste"]
        self.assertEqual(w["cancelled_job_billable_min"], 1)
        self.assertEqual(w["failed_job_billable_min"], 3)
        self.assertEqual(w["rerun_attempt_billable_min"], 5)
        self.assertAlmostEqual(w["setup_like_step_share_pct"], 16.7)

    def test_flake_signal(self):
        self.assertEqual(self.s["waste"]["flake_signal_sha_pairs"], 1)
        test_job = next(j for j in self.s["jobs"] if j["job"] == "CI / test")
        self.assertEqual(test_job["failed_then_passed"], 1)

    def test_volume(self):
        v = self.s["volume"]
        self.assertEqual((v["attempts"], v["distinct_prs"], v["rerun_attempts"]), (5, 1, 1))

    def test_pr_feedback_time_first_attempt_only(self):
        f = self.s["feedback"]["pr_attempt1_wall_min"]
        self.assertEqual((f["n"], f["p50"], f["p90"]), (2, 2.5, 3.0))

    def test_required_checks_presence(self):
        self.assertEqual(self.s["required_checks_seen"], {"test (1/2)": True, "lint": False})

    def test_compare_warns_on_small_sample(self):
        md = run("compare", str(self.out), str(self.out)).stdout
        self.assertIn("Fewer than 30 run attempts", md)
        self.assertIn("| Billable minutes | 22 | 22 | 0 |", md)


class RatesTest(unittest.TestCase):
    CASES = {
        "github-ubuntu-24.04-x86_64-32": "linux-32core",
        "github-ubuntu-24.04-x86_64-4": "linux-4core",
        "depot-ubuntu-22.04-16": "linux-16core",
        "blacksmith-8vcpu-ubuntu-2404": "linux-8core",
        "depot-ubuntu-24.04": "linux-2core (standard)",
        "ubuntu-latest": "linux-2core (standard)",
        "ubuntu-24.04-arm": "linux-2core (standard)",
        "macos-15": "macos",
        "windows-latest": "windows",
        "self-hosted,linux,x64": "self-hosted (amortised)",
    }

    def test_example_rate_rules_match_expected_classes(self):
        rules = json.loads(RATES.read_text())["rates"]
        for label, expected in self.CASES.items():
            got = next(r["name"] for r in rules if re.search(r["match"], label, re.I))
            self.assertEqual(got, expected, label)


class SkillLayoutTest(unittest.TestCase):
    def test_frontmatter_name_matches_directory(self):
        text = (SKILL / "SKILL.md").read_text()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m, "SKILL.md must start with YAML frontmatter")
        fm = m.group(1)
        self.assertRegex(fm, r"(?m)^name: ci-cost-reduction$")
        self.assertRegex(fm, r"(?m)^description: .{50,}")

    def test_referenced_files_exist(self):
        text = (SKILL / "SKILL.md").read_text()
        # File paths only (with an extension); `scripts/ci/` etc. refer to the audited repo, not the skill.
        for ref in set(re.findall(r"`((?:references|assets|scripts)/[\w\-/]+\.\w+)`", text)):
            self.assertTrue((SKILL / ref).exists(), ref)


if __name__ == "__main__":
    unittest.main()
