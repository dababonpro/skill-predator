# skill-predator

Hunt, devour, isolate, analyse, and update agent skills without losing local edits or executing untrusted code.

## Install

```bash
pip install -e .
```

Zero runtime dependencies beyond Python 3.11+ and `git` (optional `gh` CLI for GitHub search in `hunt --for`).

## Quick start

```bash
# Audit your installed/repo skills for collisions, budget, unused skills, and copy drift
skill-predator hunt --root .

# Search stomach, marketplaces, GitHub, and skills.sh before writing a new skill
skill-predator hunt --root . --for "systematic debugging"

# Clone and isolate an external skill pack into the stomach (~/.skill-sources by default)
skill-predator devour --root . --url https://github.com/obra/superpowers

# A GitHub folder link clones only that folder (sparse checkout), for packs inside a monorepo.
# Each skill's licence is read from the nearest LICENSE file up to the pack root.
skill-predator devour --root . --url https://github.com/cursor/plugins/tree/main/pstack

# Or copy a local pack. Pass only one of --url or --path.
skill-predator devour --root . --path ~/packs/some-skill

# Compare devoured prey against local skills. The report opens with a summary, a reading
# order grouped by each prey's nearest local rival, and a list of short prompt-only skills.
skill-predator analyse --root .

# Mine session transcripts for skill upgrades (evolve) and combinations (fuse)
skill-predator evolve --root . --min-cooccur 2

# Check adopted skills for upstream updates (3-way merge) and copy drift
skill-predator update --root . --fetch

# Dry-run an archive move. Add --apply to move the skill out of skills/.
skill-predator excrete --root . --skill old-skill --winner debugging

# Sort two skill folders before merging them by hand
skill-predator merge-plan --root . --ours debugging --theirs ~/.skill-sources/pack/debugging
```

## Configuration (`skill-predator.json`)

Place an optional `skill-predator.json` in your workspace root to customize paths:

```json
{
  "skills_dir": "skills",
  "lock_file": "skills/sources.json",
  "stomach_dir": "~/.skill-sources",
  "out_dir": "reviews/skills",
  "archive_dir": "archive/skills",
  "scan_roots": ["~/code"]
}
```

Relative paths are resolved from the workspace root (`--root`). `~` is expanded. If you set `skills_dir` and omit `lock_file`, the lock file is `<skills_dir>/sources.json`. `projects_dir`, `plugins_dir`, and `marketplaces_dir` use the same path rules; their defaults are under `~/.claude/`.

## Safety model

- **Fierce in the stomach, careful in the body:** `hunt`, `analyse`, `evolve`, and `update` write markdown reports and do not edit `skills/<name>/`. `evolve` mines session transcripts for silent misses, tool errors while a skill was the latest one invoked, and skills that share at least `--min-cooccur` sessions. A fuse line is a proposal in that report. `update` proposes a 3-way merge in the report and does not apply it. `update --fetch` may `git pull --ff-only` stomach clones and records a fetch failure in the report. `devour` copies or clones into the stomach and may update prey entries in the lock file (`skills/sources.json` by default).
- **Isolate code; never run it:** Every script, hook, installer, dependency manifest, and network call in devoured prey is listed and marked `isolated` without execution. `git clone` and `git pull` run with repository hooks disabled and interactive prompts off. Symlinks and Windows junctions inside a pack are not followed or copied.
- **Excrete means archive outside `skills/`:** Without `--apply`, excrete prints the planned `archive/skills/<name>/` path and moves nothing. With `--apply`, the skill moves there and gains a `POINTER.md`.
- **Offline search:** `hunt --for` still writes a report when `gh` or skills.sh is unavailable. The failure is listed under `## Search notes`.

## Licence

MIT
