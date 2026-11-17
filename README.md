# ci-cost-reduction skill

An agent skill that audits a repository's CI, measures a baseline (compute
minutes, estimated cost, PR feedback time, queue time, flakiness), proposes the
highest-value changes, and implements them as small reviewable steps. It keeps
required checks working and runs the full test suite whenever affected-test
selection is unsure. Then it verifies correctness, feedback time and cost.

It draws on [OpenClaw's CI cost-reduction write-up](https://vincent.build/openclaw-ci-cost-reduction/)
and their [CI implementation](https://github.com/openclaw/openclaw/blob/a7d05c7af04aa17ac57c994da238d0c60f058ca6/.github/workflows/ci.yml),
but treats every technique as a hypothesis to check against the target repo's
own data.

## Install

With the [`skills`](https://github.com/vercel-labs/skills) CLI:

```bash
# from GitHub
npx skills add leonardwongly/ci-cost-reduction-skill

# into your user directory for Claude Code only, no prompts
npx skills add leonardwongly/ci-cost-reduction-skill -g -a claude-code -y

# from a local clone
npx skills add ./ci-cost-reduction-skill -g -a claude-code -y
```

Without the CLI, copy `skills/ci-cost-reduction/` to `~/.claude/skills/` (all
projects) or `.claude/skills/` (one project).

## Use

Ask your agent something like:

> Our GitHub Actions bill doubled and PR checks take 25 minutes. Audit the CI,
> measure a baseline, and propose what to change.

The skill works in six phases: inventory, baseline, diagnosis, proposal (it
stops for your approval here), implementation, and verification. See
[`skills/ci-cost-reduction/SKILL.md`](skills/ci-cost-reduction/SKILL.md).

The bundled baseline script works on its own too. It needs only Python 3 and a
GitHub token with `actions:read`:

```bash
python3 skills/ci-cost-reduction/scripts/ci_baseline.py fetch --repo OWNER/REPO \
  --since 2026-10-20 --until 2026-11-03 --out baseline.jsonl
python3 skills/ci-cost-reduction/scripts/ci_baseline.py analyze baseline.jsonl \
  --rates rates.json --out baseline.json --md baseline.md
python3 skills/ci-cost-reduction/scripts/ci_baseline.py compare baseline.json after.json
```

## Layout

```
skills/ci-cost-reduction/
  SKILL.md                       workflow and principles
  references/levers.md           cost/speed levers with evidence, risk, verification
  references/required-checks.md  keeping branch protection working with conditional jobs
  references/affected-tests.md   safe test selection: fail closed, shadow mode, backtest
  references/measurement.md      metric definitions, pitfalls, other CI providers
  references/openclaw-playbook.md  what OpenClaw did and what may not transfer
  scripts/ci_baseline.py         fetch / analyze / compare (stdlib only)
  assets/rates.example.json      example runner rates (replace with real ones)
  assets/proposal-template.md    proposal / approval-gate template
tests/                           unit tests for the script and skill layout
```

## Develop

```bash
python3 -m unittest discover -s tests
npx -y skills@1.9.0 add . --list      # confirm the CLI discovers the skill
```

## License

[MIT](LICENSE)
