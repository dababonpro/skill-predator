---
name: skill-predator
description: "Hunts, devours and absorbs agent skills. Hunts: before a new skill is written, or when a skill fails to fire, searches skill sources for one that already does the job, and flags weak or duplicate skills already installed. Devours: swallows whole skill packs from the web or plugins into a separate stomach folder, pinned to a commit. Isolates every script, hook and installer it swallows and never runs them. Analyses each skill against its rivals, then proposes to mimic its best steps into an existing skill with credit, evolve two skills into a stronger one, keep it as new, or excrete the loser to the archive. Keeps absorbed skills current with upstream without losing local edits. Changes to live skills wait for approval. Use when the user clones or installs skills or a skill pack, asks whether a skill exists, wants skills merged, deduplicated, cleaned up or updated, or a copied skill has drifted."
---

# Skill Predator

Hunt, devour, isolate, analyse, and selectively absorb agent skills without losing local edits or executing untrusted code.

## Modes

| Mode | Touches | Asks first |
|---|---|---|
| `hunt` | `reviews/skills/YYYY-MM-DD-hunt.md` | No |
| `devour` | `~/.skill-sources/<pack>` (or configured stomach) | No |
| `isolate` | Stomach inventory (runs automatically in `devour`) | No |
| `analyse` | Reports and proposals | No |
| `mimic` | Live `skills/` (merge steps with credit) | Yes |
| `evolve` | Live `skills/` (stronger merged skill) | Yes |
| `excrete` | Live `skills/` -> `archive/skills/<name>/` | Yes |

## Rules

1. **Fierce in the stomach, careful in the body.** `hunt`, `analyse`, and `update` never edit `skills/<name>/`. `update` only writes a report; `update --fetch` may pull stomach clones and records a fetch failure instead of pretending the pull worked. `devour` copies or clones into the stomach and may update prey metadata in `skills/sources.json`. It does not edit skill content. Any change to a live skill (`mimic`, `evolve`, `excrete --apply`, or disabling a plugin skill in `~/.claude/settings.json`) is proposed with a diff and applied only after explicit user approval. `excrete` without `--apply` prints the archive path and moves nothing.
2. **Isolate code; never run it.** List every script, hook, installer, dependency list, and network call in devoured prey. Never execute them, and never move a skill carrying them into `skills/` until the user has read them. `git clone` and `git pull` run with repository hooks disabled and interactive prompts off. Symlinks and Windows junctions are not followed or copied.
3. **Deterministic report first.** Run the CLI before judging:

```bash
skill-predator hunt --root . [--for "<job in words>"]
skill-predator devour --root . [--url <git url> | --path <folder>]
skill-predator analyse --root . [--pack <name>]
skill-predator update --root . [--fetch]
skill-predator excrete --root . --skill <name> --winner <winner> [--apply]
skill-predator merge-plan --root . --ours <skill> --theirs <folder>
```

4. **Track provenance in `skills/sources.json`.** Record origin, licence, folder-content SHA-256, and copy transforms (`verbatim`, `strip-footer`, `replace`).
5. **Excrete outside `skills/`.** Move retired skills to `archive/skills/<name>/` with `POINTER.md` (`skill-predator excrete --skill <name> --winner <winner> --apply`) so active skill junctions never load them into sessions.

## Verdicts (when analysing prey)

- **Mimic:** A rival already does the job and prey adds steps/lessons it lacks. Rewrite new sections in house style with one credit line (`Adapted from <repo>, <licence>`). Record prey as `mimicked`.
- **Evolve:** Prey does the job better, or combining both beats either. Use `skill-predator merge-plan --ours <skill> --theirs <folder>` to sort files into redundant, complementary, and conflicting; draft one stronger skill, test against 2-3 real past prompts, then excrete the loser.
- **New:** No rival covers the job and it passes when-not-to-create rules (not one-off, not a 1-line rule, teaches non-obvious steps/scripts). Copy to `skills/` and record in `skills/sources.json`.
- **Skip:** Already covered (~80%+), incompatible licence (GPL/CC-BY-SA cannot be mimicked into differently-licensed skills), or trivial. Record `skipped` with reason in `skills/sources.json`.
