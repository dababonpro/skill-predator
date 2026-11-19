"""CLI entrypoint for skill-predator."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from skill_predator.core import (
    PredatorConfig,
    devour_packs,
    excrete_skill,
    sort_merge_files,
    write_analyse,
    write_evolve,
    write_hunt,
    write_hunt_for,
    write_update,
)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--root", default=".", help="workspace root containing skills/ (default: .)")
    p.add_argument("--today", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--stomach", default=None, help="stomach folder for devoured packs (default: ~/.skill-sources)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skill-predator", description="Hunt, devour, isolate, analyse, and update agent skills.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_hunt = sub.add_parser("hunt", help="audit territory skills or search outside for a job (--for)")
    _add_common(p_hunt)
    p_hunt.add_argument("--for", dest="for_job", default=None, help="job description for outside hunt")
    p_hunt.add_argument("--scan", action="append", default=None, help="repo folder to scan (repeatable)")
    p_hunt.add_argument("--projects", default=None, help="Claude Code transcripts folder")
    p_hunt.add_argument("--plugins", default=None, help="plugin cache folder")
    p_hunt.add_argument("--marketplaces", default=None, help="plugin marketplaces folder")

    p_devour = sub.add_parser("devour", help="devour and isolate skill pack(s) into the stomach")
    _add_common(p_devour)
    p_devour.add_argument("--url", default=None, help="git URL to clone into the stomach")
    p_devour.add_argument("--path", default=None, help="local pack folder to copy/register in the stomach")

    p_analyse = sub.add_parser("analyse", help="compare stomach prey against territory rivals")
    _add_common(p_analyse)
    p_analyse.add_argument("--pack", default=None, help="single stomach pack name to analyse")
    p_analyse.add_argument("--scan", action="append", default=None, help="repo folder to scan (repeatable)")
    p_analyse.add_argument("--plugins", default=None, help="plugin cache folder")

    p_update = sub.add_parser("update", help="check adopted skills against upstream and copies for drift")
    _add_common(p_update)
    p_update.add_argument("--fetch", action="store_true", help="git pull --ff-only in stomach packs first")

    p_evolve = sub.add_parser("evolve", help="propose data-driven skill upgrades (evolve) and combinations (fuse)")
    _add_common(p_evolve)
    p_evolve.add_argument("--projects", default=None, help="Claude Code transcripts folder")
    p_evolve.add_argument("--min-cooccur", type=int, default=2, help="minimum shared sessions to propose fusion")

    p_excrete = sub.add_parser("excrete", help="archive a losing skill outside skills/")
    _add_common(p_excrete)
    p_excrete.add_argument("--skill", required=True, help="skill name in skills/ to archive")
    p_excrete.add_argument("--winner", required=True, help="winning skill name or reference")
    p_excrete.add_argument("--apply", action="store_true", help="execute the archive move (default: dry-run)")

    p_merge = sub.add_parser("merge-plan", help="sort files of two skills into redundant/complementary/conflicting")
    p_merge.add_argument("--root", default=".", help="workspace root")
    p_merge.add_argument("--ours", required=True, help="skill name under skills/")
    p_merge.add_argument("--theirs", required=True, help="path to prey skill folder")

    args = parser.parse_args(argv)
    if args.cmd == "devour" and args.url and args.path:
        parser.error("pass only one of --url and --path")
    today = getattr(args, "today", None) or date.today().isoformat()
    try:
        date.fromisoformat(today)
    except ValueError:
        parser.error(f"--today must be YYYY-MM-DD, got {today!r}")

    overrides: dict[str, object] = {}
    if getattr(args, "stomach", None):
        overrides["stomach_dir"] = Path(args.stomach).expanduser()
    if getattr(args, "scan", None):
        overrides["scan_roots"] = [Path(s).expanduser() for s in args.scan]
    if getattr(args, "projects", None):
        overrides["projects_dir"] = Path(args.projects).expanduser()
    if getattr(args, "plugins", None):
        overrides["plugins_dir"] = Path(args.plugins).expanduser()
    if getattr(args, "marketplaces", None):
        overrides["marketplaces_dir"] = Path(args.marketplaces).expanduser()

    try:
        cfg = PredatorConfig.load(Path(args.root).expanduser(), **overrides)
        if args.cmd == "hunt":
            out = write_hunt_for(cfg, today, args.for_job) if args.for_job else write_hunt(cfg, today)
            print(f"wrote {out}")
            return 0
        if args.cmd == "devour":
            out = devour_packs(cfg, today, url=args.url, path=Path(args.path).expanduser() if args.path else None)
            print(f"wrote {out}")
            return 0
        if args.cmd == "analyse":
            out = write_analyse(cfg, today, pack=args.pack)
            print(f"wrote {out}")
            return 0
        if args.cmd == "update":
            out = write_update(cfg, today, fetch=args.fetch)
            print(f"wrote {out}")
            return 0
        if args.cmd == "evolve":
            out = write_evolve(cfg, today, min_cooccur=args.min_cooccur)
            print(f"wrote {out}")
            return 0
        if args.cmd == "excrete":
            dest = excrete_skill(cfg, today, args.skill, args.winner, apply=args.apply)
            verb = "excreted" if args.apply else "dry-run (pass --apply to move)"
            print(f"{verb}: {args.skill} -> {dest}")
            return 0
        if args.cmd == "merge-plan":
            if "/" in args.ours or "\\" in args.ours or args.ours in {".", ".."}:
                raise ValueError(f"Skill name must be a single folder under skills/: {args.ours}")
            plan = sort_merge_files(cfg.skills_dir / args.ours, Path(args.theirs).expanduser())
            print(json.dumps(plan, indent=2))
            return 0
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
