import json
import os
import subprocess
from pathlib import Path

import pytest

from skill_predator.cli import main
from skill_predator.core import (
    PredatorConfig,
    _git_head_commit,
    apply_transform,
    devour_packs,
    excrete_skill,
    isolate_skill_folder,
    iter_files,
    parse_skill_md,
    search_github_skills,
    search_skills_sh,
    sort_merge_files,
    three_way_merge,
    write_analyse,
    write_hunt,
    write_hunt_for,
    write_update,
)


def _write_skill(base: Path, name: str, description: str, body: str = "## Steps\n1. Do work.\n") -> Path:
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    md = folder / "SKILL.md"
    md.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}", encoding="utf-8")
    return md


def test_config_loads_json_and_overrides(tmp_path: Path):
    (tmp_path / "skill-predator.json").write_text(
        json.dumps({"stomach_dir": "custom-stomach", "scan_roots": ["repos"]}),
        encoding="utf-8",
    )
    cfg = PredatorConfig.load(tmp_path)
    assert cfg.stomach_dir == (tmp_path / "custom-stomach").resolve()
    assert cfg.scan_roots == [(tmp_path / "repos").resolve()]


def test_hunt_devour_analyse_update_and_excrete(tmp_path: Path):
    root = tmp_path / "ws"
    stomach = tmp_path / "stomach"
    _write_skill(root / "skills", "own-debug", "Debug test failures systematically with root cause tracing.")
    _write_skill(root / "skills", "old-skill", "Legacy workflow to retire.")

    pack = stomach / "pack-a"
    pack.mkdir(parents=True)
    (pack / "LICENSE").write_text("MIT License\nPermission is hereby granted, free of charge", encoding="utf-8")
    prey_md = _write_skill(pack / "skills", "sys-debug", "Debug test failures systematically and verify migrations.")
    (prey_md.parent / "scripts").mkdir()
    (prey_md.parent / "scripts" / "run.py").write_text("import requests\nrequests.get('https://example.com')\n", encoding="utf-8")

    cfg = PredatorConfig.load(
        root,
        stomach_dir=stomach,
        projects_dir=tmp_path / "none",
        plugins_dir=tmp_path / "none",
        marketplaces_dir=tmp_path / "none",
    )
    (cfg.skills_dir / "sources.json").write_text(
        json.dumps({"skills": {"own-debug": {"origin": "own"}, "old-skill": {"origin": "own"}}, "prey": {}}),
        encoding="utf-8",
    )

    assert isolate_skill_folder(prey_md.parent)["isolated"] is True
    devour_out = devour_packs(cfg, "2026-11-19")
    assert "ISOLATED" in devour_out.read_text(encoding="utf-8")

    analyse_out = write_analyse(cfg, "2026-11-19")
    assert "Mimic / Evolve" in analyse_out.read_text(encoding="utf-8")

    hunt_out = write_hunt(cfg, "2026-11-19")
    assert "## Budget" in hunt_out.read_text(encoding="utf-8")

    hunt_for_out = write_hunt_for(cfg, "2026-11-19", "debug failures systematically", include_public=False)
    assert "sys-debug" in hunt_for_out.read_text(encoding="utf-8")

    base = "# Skill\n\n## A\nOne\n\n## B\nTwo\n"
    ours = "# Skill\n\n## A\nLocal\n\n## B\nTwo\n"
    theirs = "# Skill\n\n## A\nOne\n\n## B\nUpstream\n"
    merged, conflict = three_way_merge(base, ours, theirs)
    assert not conflict
    assert "Local" in merged and "Upstream" in merged

    sorted_files = sort_merge_files(root / "skills" / "own-debug", prey_md.parent)
    assert sorted_files["conflicting"] == ["SKILL.md"]

    dest = excrete_skill(cfg, "2026-11-19", "old-skill", "own-debug", apply=True)
    assert not (root / "skills" / "old-skill").exists()
    assert (dest / "POINTER.md").is_file()

    assert main(["hunt", "--root", str(root), "--today", "2026-11-19", "--stomach", str(stomach), "--projects", str(tmp_path / "none"), "--plugins", str(tmp_path / "none")]) == 0


def test_config_override_and_skills_dir_are_root_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    (tmp_path / "skill-predator.json").write_text(
        json.dumps({"skills_dir": "custom-skills", "lock_file": "locks/sources.json"}),
        encoding="utf-8",
    )
    cfg = PredatorConfig.load(tmp_path, stomach_dir="belly", scan_roots=["more"])
    assert cfg.stomach_dir == (tmp_path / "belly").resolve()
    assert cfg.scan_roots == [(tmp_path / "more").resolve()]
    assert cfg.skills_dir == (tmp_path / "custom-skills").resolve()
    assert cfg.lock_file == (tmp_path / "locks" / "sources.json").resolve()

    (tmp_path / "skill-predator.json").write_text(json.dumps({"skills_dir": "only-skills"}), encoding="utf-8")
    moved = PredatorConfig.load(tmp_path)
    assert moved.lock_file == moved.skills_dir / "sources.json"

    (tmp_path / "skill-predator.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        PredatorConfig.load(tmp_path)


def test_parse_quoted_description_and_shipped_skill():
    root = Path(__file__).resolve().parents[1]
    shipped = parse_skill_md(root / "skills" / "skill-predator" / "SKILL.md")
    assert shipped["name"] == "skill-predator"
    assert 0 < len(shipped["description"]) <= 1024
    assert "Hunts:" in shipped["description"]

    folder = root / "skills" / "skill-predator"
    info = parse_skill_md(folder / "SKILL.md")
    assert info["description"].startswith("Hunts, devours")
    assert not info["description"].startswith('"')


def test_apply_transform_modes():
    text = "# T\r\n\r\n*Written for middle*\r\n\r\nKeep.\r\n\r\n*Written for the pack*\r\n"
    verbatim = apply_transform(text, "verbatim")
    assert verbatim == "# T\n\n*Written for middle*\n\nKeep.\n\n*Written for the pack*\n"
    stripped = apply_transform(text, "strip-footer")
    assert "*Written for the pack*" not in stripped
    assert "*Written for middle*" in stripped
    assert "Keep." in stripped
    replaced = apply_transform("Hello OLD\n\n*Written for x*\n", "replace", {"OLD": "NEW"})
    assert replaced == "Hello NEW\n"
    with pytest.raises(ValueError):
        apply_transform("x", "nope")


def test_three_way_merge_conflict():
    merged, conflict = three_way_merge("line\n", "ours\n", "theirs\n")
    assert conflict
    assert "<<<<<<<" in merged
    assert "ours" in merged and "theirs" in merged


def test_devour_path_does_not_run_scripts(tmp_path: Path):
    root = tmp_path / "ws"
    pack = tmp_path / "external"
    marker = tmp_path / "pwned"
    prey_md = _write_skill(pack, "prey", "Debug failures with a helper script.")
    (prey_md.parent / "run.py").write_text(
        "from pathlib import Path\nPath(%r).write_text('ran')\n" % str(marker),
        encoding="utf-8",
    )
    (prey_md.parent / "install.sh").write_text("echo hi\n", encoding="utf-8")
    cfg = PredatorConfig.load(root, stomach_dir=tmp_path / "stomach")
    before = prey_md.read_bytes()
    out = devour_packs(cfg, "2026-11-19", path=pack)
    report = out.read_text(encoding="utf-8")
    assert "ISOLATED" in report
    assert not marker.exists()
    copied = tmp_path / "stomach" / "external" / "prey" / "run.py"
    assert copied.is_file()
    assert prey_md.read_bytes() == before
    iso = isolate_skill_folder(copied.parent)
    assert "run.py" in iso["scripts"]
    assert "install.sh" in iso["hooks_or_installers"]


def test_devour_rejects_url_and_path(tmp_path: Path):
    with pytest.raises(SystemExit) as caught:
        main([
            "devour", "--root", str(tmp_path), "--url", "https://example.invalid/a.git", "--path", str(tmp_path),
        ])
    assert caught.value.code == 2


def test_devour_url_clone_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "ws"
    (root / "skills").mkdir(parents=True)
    cfg = PredatorConfig.load(root, stomach_dir=tmp_path / "stomach")

    class Result:
        returncode = 1
        stderr = "fatal: repository not found"
        stdout = ""

    monkeypatch.setattr("skill_predator.core._run_git", lambda args, timeout=60: Result())
    with pytest.raises(RuntimeError, match="git clone failed"):
        devour_packs(cfg, "2026-11-19", url="https://example.invalid/nope.git")


def test_licence_in_pack_subfolder_is_found(tmp_path: Path):
    # cursor/plugins keeps pstack's LICENSE in pstack/, not at the repo root; devour read "unknown".
    from skill_predator.core import detect_pack_licence, detect_skill_licence

    pack = tmp_path / "plugins"
    sub = pack / "pstack"
    skill = _write_skill(sub / "skills", "correct", "Fix repeated agent mistakes.").parent
    (sub / "LICENSE").write_text("MIT License\nPermission is hereby granted, free of charge", encoding="utf-8")
    other = _write_skill(pack / "other" / "skills", "x", "Unlicensed skill.").parent

    assert detect_pack_licence(pack) == "unknown"
    assert detect_skill_licence(skill, pack) == "MIT"
    assert detect_skill_licence(other, pack) == "unknown"

    (pack / "LICENCE.txt").write_text("Apache License\nVersion 2.0", encoding="utf-8")
    assert detect_pack_licence(pack) == "Apache-2.0"
    assert detect_skill_licence(other, pack) == "Apache-2.0"
    assert detect_skill_licence(skill, pack) == "MIT"  # nearest file wins

    cfg = PredatorConfig.load(tmp_path / "ws", stomach_dir=tmp_path)
    cfg.skills_dir.mkdir(parents=True, exist_ok=True)
    text = devour_packs(cfg, "2026-11-23", path=pack).read_text(encoding="utf-8")
    assert "Licence: `Apache-2.0`" in text
    assert "`correct`: **clean** (licence=MIT)" in text
    lock = json.loads((cfg.skills_dir / "sources.json").read_text(encoding="utf-8"))
    assert lock["prey"]["plugins/correct"]["licence"] == "MIT"


def test_github_folder_link_is_sparse_cloned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import skill_predator.core as core

    assert core.parse_github_tree_url("https://github.com/cursor/plugins/tree/main/pstack") == (
        "https://github.com/cursor/plugins.git", "main", "pstack")
    assert core.parse_github_tree_url("https://github.com/o/r/tree/v2/a/b/") == ("https://github.com/o/r.git", "v2", "a/b")
    assert core.parse_github_tree_url("https://github.com/obra/superpowers") is None
    assert core._pack_name_from_url("https://github.com/cursor/plugins/tree/main/pstack") == "pstack"
    assert core._pack_name_from_url("https://github.com/obra/superpowers.git") == "superpowers"

    calls = []

    class Done:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(core, "_run_git", lambda args, timeout=60: calls.append(args) or Done())
    core._clone_pack("https://github.com/cursor/plugins/tree/main/pstack", tmp_path / "pstack")
    assert calls[0][:4] == ["clone", "--filter=blob:none", "--no-checkout", "--"]
    assert calls[1][-4:] == ["sparse-checkout", "set", "--", "pstack"]
    assert calls[2][-3:] == ["checkout", "main", "--"]

    calls.clear()
    core._clone_pack("https://github.com/obra/superpowers", tmp_path / "sp")
    assert calls == [["clone", "--", "https://github.com/obra/superpowers", str(tmp_path / "sp")]]


def test_analyse_groups_large_pack_by_rival(tmp_path: Path):
    # On a 61-skill pack plain word overlap gave every skill the same verdict and no reading order.
    long_body = "## Steps\n" + " ".join(f"step{i}" for i in range(120)) + "\n"
    root = tmp_path / "ws"
    _write_skill(root / "skills", "feature-map", "Record what each feature must keep doing and prove it with a regression check.", long_body)
    _write_skill(root / "skills", "queue-run", "Work through a batch list one item at a time with a queue file.", long_body)

    stomach = tmp_path / "stomach"
    skills = stomach / "big" / "skills"
    _write_skill(skills, "maintain-map", "Keep the feature map honest with a regression check per feature.", long_body)
    _write_skill(skills, "batch-worker", "Process a batch list item by item from a queue.", long_body)
    _write_skill(skills, "tiny", "Restate the goal in your own words.", "Restate the goal.\n")
    for i in range(8):
        _write_skill(skills, f"loner-{i}", f"Unrelated topic zeta{i} omega{i}.", long_body)

    cfg = PredatorConfig.load(root, stomach_dir=stomach, projects_dir=tmp_path / "none",
                              plugins_dir=tmp_path / "none", marketplaces_dir=tmp_path / "none")
    (cfg.skills_dir / "sources.json").write_text(json.dumps({"skills": {}, "prey": {}}), encoding="utf-8")

    text = write_analyse(cfg, "2026-11-23").read_text(encoding="utf-8")
    assert "## Summary" in text and "11 skill(s)" in text
    order = text.split("## Read in this order", 1)[1].split("## Short prompts", 1)[0]
    assert "- `feature-map` (own): `big/maintain-map`" in order
    assert "- `queue-run` (own): `big/batch-worker`" in order
    assert "No rival above" in order and "`big/loner-0`" in order
    assert "`big/tiny`" in text.split("## Short prompts", 1)[1].split("## Each skill", 1)[0]
    assert "short prompt" in text.split("### `big/tiny`", 1)[1]


def test_excrete_dry_run_does_not_move(tmp_path: Path):
    root = tmp_path / "ws"
    _write_skill(root / "skills", "old-skill", "Legacy workflow to retire.")
    cfg = PredatorConfig.load(root, stomach_dir=tmp_path / "stomach")
    src = root / "skills" / "old-skill"
    before = (src / "SKILL.md").read_bytes()
    dest = excrete_skill(cfg, "2026-11-19", "old-skill", "winner", apply=False)
    assert dest == cfg.archive_dir / "old-skill"
    assert not dest.exists()
    assert (src / "SKILL.md").read_bytes() == before
    assert main(["excrete", "--root", str(root), "--today", "2026-11-19", "--skill", "old-skill", "--winner", "winner"]) == 0
    assert src.is_dir()
    with pytest.raises(ValueError):
        excrete_skill(cfg, "2026-11-19", "../old-skill", "winner", apply=True)
    assert main(["excrete", "--root", str(root), "--skill", "missing", "--winner", "winner"]) == 1


def test_update_conflict_and_clean_merge_do_not_edit_skills(tmp_path: Path):
    root = tmp_path / "ws"
    conflict_md = _write_skill(root / "skills", "adopted", "Keep local edits while tracking upstream.")
    conflict_md.write_text("# Skill\n\n## A\nLocal\n", encoding="utf-8")
    clean_md = _write_skill(root / "skills", "clean", "Keep local edits while tracking upstream.")
    ours = "# Skill\n\n## A\nLocal\n\n## B\nTwo\n"
    clean_md.write_text(ours, encoding="utf-8")
    stomach = tmp_path / "stomach"
    up_conflict = _write_skill(stomach / "pack", "adopted", "upstream")
    up_conflict.write_text("# Skill\n\n## A\nUpstream\n", encoding="utf-8")
    up_clean = _write_skill(stomach / "pack", "clean", "upstream")
    up_clean.write_text("# Skill\n\n## A\nOne\n\n## B\nUpstream\n", encoding="utf-8")
    cfg = PredatorConfig.load(root, stomach_dir=stomach, projects_dir=tmp_path / "none", plugins_dir=tmp_path / "none")
    (cfg.skills_dir / "sources.json").write_text(json.dumps({
        "skills": {
            "adopted": {"origin": "adopted", "base_text": "# Skill\n\n## A\nBase\n", "base_sha256": "0"},
            "clean": {
                "origin": "adopted",
                "base_text": "# Skill\n\n## A\nOne\n\n## B\nTwo\n",
                "base_sha256": "0",
            },
        },
        "prey": {},
    }), encoding="utf-8")
    before_conflict = conflict_md.read_bytes()
    before_clean = clean_md.read_bytes()
    out = write_update(cfg, "2026-11-19", fetch=False)
    report = out.read_text(encoding="utf-8")
    assert conflict_md.read_bytes() == before_conflict
    assert clean_md.read_bytes() == before_clean
    assert "CONFLICT" in report
    assert "<<<<<<<" in report
    assert "clean 3-way merge proposed" in report


def test_update_fetch_failure_is_reported(tmp_path: Path):
    root = tmp_path / "ws"
    (root / "skills").mkdir(parents=True)
    pack = tmp_path / "stomach" / "broken"
    pack.mkdir(parents=True)
    (pack / ".git").mkdir()
    cfg = PredatorConfig.load(root, stomach_dir=tmp_path / "stomach")
    out = write_update(cfg, "2026-11-19", fetch=True)
    assert "fetch failed" in out.read_text(encoding="utf-8")


def test_public_search_offline_and_odd_payloads(monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("skill_predator.core.subprocess.run", boom)
    hits, note = search_github_skills("debug")
    assert hits == []
    assert note is not None and "unavailable" in note

    class Odd:
        returncode = 0
        stdout = "123"
        stderr = ""

    monkeypatch.setattr("skill_predator.core.subprocess.run", lambda *args, **kwargs: Odd())
    hits, note = search_github_skills("debug")
    assert hits == []
    assert note is not None and "unexpected" in note

    class Licensed:
        returncode = 0
        stdout = json.dumps([{
            "fullName": "a/b",
            "description": "debug things",
            "stargazersCount": 3,
            "updatedAt": "2026-01-02T00:00:00Z",
            "license": "mit",
            "url": "https://example.com",
        }])
        stderr = ""

    monkeypatch.setattr("skill_predator.core.subprocess.run", lambda *args, **kwargs: Licensed())
    hits, note = search_github_skills("debug")
    assert note is None
    assert hits[0]["licence"] == "mit"

    def boom_url(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("skill_predator.core.urllib.request.urlopen", boom_url)
    hits, note = search_skills_sh("debug")
    assert hits == []
    assert note is not None and "unavailable" in note


def test_hunt_for_records_offline_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "ws"
    skill = _write_skill(root / "skills", "own-debug", "Debug test failures systematically with root cause tracing.")
    before = skill.read_bytes()
    cfg = PredatorConfig.load(
        root,
        stomach_dir=tmp_path / "st",
        projects_dir=tmp_path / "none",
        plugins_dir=tmp_path / "none",
        marketplaces_dir=tmp_path / "none",
    )

    def boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("skill_predator.core.subprocess.run", boom)
    monkeypatch.setattr("skill_predator.core.urllib.request.urlopen", boom)
    out = write_hunt_for(cfg, "2026-11-19", "debug failures systematically", include_public=True)
    text = out.read_text(encoding="utf-8")
    assert "own-debug" in text
    assert "gh search unavailable" in text
    assert "skills.sh unavailable" in text
    assert skill.read_bytes() == before


def test_devour_does_not_follow_or_delete_through_junctions(tmp_path: Path):
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")
    root = tmp_path / "ws"
    pack = tmp_path / "external"
    skill = pack / "prey"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: prey\ndescription: Debug with care.\n---\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_text("print('no')\n", encoding="utf-8")
    link = skill / "linked"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
    if created.returncode != 0:
        pytest.skip("could not create a junction")
    stomach = tmp_path / "stomach"
    cfg = PredatorConfig.load(root, stomach_dir=stomach)
    try:
        assert "secret.py" not in [p.name for p in iter_files(skill)]
        devour_packs(cfg, "2026-11-19", path=pack)
        copied = stomach / "external"
        assert not (copied / "prey" / "linked" / "secret.py").exists()
        assert secret.is_file()
        planted = copied / "planted"
        replanted = subprocess.run(["cmd", "/c", "mklink", "/J", str(planted), str(outside)], capture_output=True, text=True)
        assert replanted.returncode == 0
        devour_packs(cfg, "2026-11-19", path=pack)
        assert secret.is_file()
        assert not (copied / "prey" / "linked" / "secret.py").exists()
    finally:
        subprocess.run(["cmd", "/c", "rmdir", str(link)], check=False)
        subprocess.run(["cmd", "/c", "rmdir", str(stomach / "external" / "planted")], check=False)


def test_isolate_skips_symlinks(tmp_path: Path):
    skill = tmp_path / "prey"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: prey\ndescription: x\n---\n", encoding="utf-8")
    outside = tmp_path / "secret.py"
    outside.write_text("import os\nos.system('echo pwned')\n", encoding="utf-8")
    link = skill / "secret.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not permitted")
    iso = isolate_skill_folder(skill)
    assert "secret.py" not in iso["scripts"]


def test_git_head_commit_uses_this_checkout():
    repo = Path(__file__).resolve().parents[1]
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    commit = _git_head_commit(repo)
    assert commit.isalnum()


def test_evolve_and_fuse_evidence(tmp_path: Path):
    from skill_predator.core import write_evolve

    root = tmp_path / "ws"
    projects = tmp_path / "projects" / "repo-a"
    projects.mkdir(parents=True)

    _write_skill(root / "skills", "alpha-skill", "Manage notes and create structured markdown files.")
    _write_skill(root / "skills", "beta-skill", "Update notes and keep structured markdown links.")
    (root / "skills" / "sources.json").write_text(
        json.dumps({
            "skills": {
                "alpha-skill": {"origin": "own", "signatures": [r"note_helper\.py"]},
                "beta-skill": {"origin": "own"},
            },
            "prey": {},
        }),
        encoding="utf-8",
    )

    # Session 1 & 2: both skills run. The error lands while alpha-skill is the latest one.
    for idx in (1, 2):
        (projects / f"sess-{idx}.jsonl").write_text(
            "\n".join([
                json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "alpha-skill"}}]}}),
                json.dumps({"message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]}}),
                json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "beta-skill"}}]}}),
            ]) + "\n",
            encoding="utf-8",
        )

    # Session 3: silent miss for alpha-skill
    (projects / "sess-3.jsonl").write_text(
        json.dumps({"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python note_helper.py"}}]}}),
        encoding="utf-8",
    )

    cfg = PredatorConfig.load(root, projects_dir=tmp_path / "projects", plugins_dir=tmp_path / "none", stomach_dir=tmp_path / "none")
    out = write_evolve(cfg, "2026-11-19", min_cooccur=2)
    text = out.read_text(encoding="utf-8")
    evolve = text.split("## Fuse candidates")[0]
    assert "**alpha-skill**" in evolve
    assert "1 silent miss(es)" in evolve
    assert "2 tool error(s)" in evolve
    assert not any("**beta-skill**" in ln and "tool error" in ln for ln in evolve.splitlines())
    assert "**alpha-skill** + **beta-skill**" in text
    assert "co-occurred in 2 session(s)" in text
    assert "overlapping" not in text


def test_fuse_requires_min_cooccur_even_when_descriptions_overlap(tmp_path: Path):
    from skill_predator.core import write_evolve

    root = tmp_path / "ws"
    projects = tmp_path / "projects" / "repo-a"
    projects.mkdir(parents=True)
    _write_skill(root / "skills", "alpha-skill", "Manage notes and create structured markdown files.")
    _write_skill(root / "skills", "beta-skill", "Update notes and keep structured markdown links.")
    (projects / "sess-1.jsonl").write_text(
        "\n".join([
            json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "alpha-skill"}}]}}),
            json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "beta-skill"}}]}}),
        ]) + "\n",
        encoding="utf-8",
    )

    cfg = PredatorConfig.load(root, projects_dir=tmp_path / "projects", plugins_dir=tmp_path / "none", stomach_dir=tmp_path / "none")
    out = write_evolve(cfg, "2026-11-19", min_cooccur=2)
    text = out.read_text(encoding="utf-8")
    assert "**alpha-skill** + **beta-skill**" not in text
    assert "no pairs with >= 2 shared sessions" in text


def test_contributing_allows_devour_to_update_the_lock_file():
    text = (Path(__file__).resolve().parents[1] / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "may update prey entries in `skills/sources.json`" in text
    assert "does not edit skill content" in text
    assert "never modify live `skills/`" not in text


def test_skill_spec_says_evolve_only_writes_a_report():
    text = (Path(__file__).resolve().parents[1] / "skills" / "skill-predator" / "SKILL.md").read_text(encoding="utf-8")
    assert "| `evolve` | `reviews/skills/YYYY-MM-DD-evolve.md` | No |" in text
    assert "| `fuse` |" not in text
    assert "`evolve` writes `reviews/skills/YYYY-MM-DD-evolve.md` and does not edit `skills/<name>/`" in text
