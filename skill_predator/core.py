"""Core deterministic engine for skill-predator (stdlib-only + git)."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "before", "by", "do", "does",
    "for", "from", "has", "have", "how", "if", "in", "into", "is", "it", "its",
    "no", "not", "of", "on", "or", "out", "so", "than", "that", "the", "their",
    "them", "then", "this", "to", "up", "use", "used", "using", "was", "were",
    "what", "when", "where", "which", "who", "will", "with", "you", "your",
})

MAX_DESCRIPTION_CHARS = 1024
JACCARD_THRESHOLD = 0.25
TRANSCRIPT_DAYS = 30
SCRIPT_EXTS = frozenset({".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".ts", ".mjs", ".cjs", ".rb", ".pl"})
DEPENDENCY_FILES = frozenset({
    "package.json", "pyproject.toml", "requirements.txt", "Pipfile",
    "Cargo.toml", "go.mod", "Makefile", "Dockerfile",
})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_HEADING_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_FOOTER_RE = re.compile(r"\n*\*Written for [^\n]*\*[ \t]*\n*\Z")
_CONFIG_PATH_KEYS = (
    "skills_dir",
    "lock_file",
    "stomach_dir",
    "out_dir",
    "archive_dir",
    "projects_dir",
    "plugins_dir",
    "marketplaces_dir",
)
_HOOK_NAME_RE = re.compile(r"(hook|install|setup)", re.IGNORECASE)
_NET_CALL_RE = re.compile(r"\b(curl|wget|Invoke-WebRequest|iwr|requests\.|httpx\.|urllib\.|fetch\()\b")


@dataclass
class PredatorConfig:
    """Configurable paths. Defaults use the workspace root and the user home directory."""

    root: Path
    skills_dir: Path = field(init=False)
    lock_file: Path = field(init=False)
    stomach_dir: Path = field(init=False)
    out_dir: Path = field(init=False)
    archive_dir: Path = field(init=False)
    scan_roots: list[Path] = field(default_factory=list)
    projects_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "projects")
    plugins_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "plugins" / "cache")
    marketplaces_dir: Path = field(default_factory=lambda: Path.home() / ".claude" / "plugins" / "marketplaces")

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.skills_dir = self.root / "skills"
        self.lock_file = self.skills_dir / "sources.json"
        self.stomach_dir = Path.home() / ".skill-sources"
        self.out_dir = self.root / "reviews" / "skills"
        self.archive_dir = self.root / "archive" / "skills"

    @classmethod
    def load(cls, root: Path, **overrides: object) -> "PredatorConfig":
        """Load optional skill-predator.json from root, then apply CLI overrides.

        Relative paths, including ``scan_roots``, are resolved from the workspace
        root. ``~`` is expanded. When ``skills_dir`` is customized and ``lock_file``
        is not, the lock file stays at ``<skills_dir>/sources.json``.
        """
        cfg = cls(root=root)
        cfg_file = cfg.root / "skill-predator.json"
        data: dict = {}
        if cfg_file.is_file():
            try:
                loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid {cfg_file.name}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"{cfg_file.name} must contain a JSON object")
            data = loaded
            for key in _CONFIG_PATH_KEYS:
                if data.get(key):
                    setattr(cfg, key, _resolve_config_path(cfg.root, data[key]))
            roots = data.get("scan_roots")
            if roots:
                if not isinstance(roots, list):
                    raise ValueError("scan_roots must be a list of paths")
                cfg.scan_roots = [_resolve_config_path(cfg.root, item) for item in roots]
        for key, val in overrides.items():
            if val is None:
                continue
            if key == "scan_roots":
                if not isinstance(val, list):
                    raise ValueError("scan_roots must be a list of paths")
                cfg.scan_roots = [_resolve_config_path(cfg.root, item) for item in val]
            elif key in _CONFIG_PATH_KEYS:
                setattr(cfg, key, _resolve_config_path(cfg.root, val))
            else:
                setattr(cfg, key, val)
        skills_customized = bool(data.get("skills_dir") or overrides.get("skills_dir") is not None)
        lock_customized = bool(data.get("lock_file") or overrides.get("lock_file") is not None)
        if skills_customized and not lock_customized:
            cfg.lock_file = cfg.skills_dir / "sources.json"
        return cfg


def _resolve_config_path(root: Path, value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"Expected a path, got {type(value).__name__}")
    raw = str(value).strip()
    if not raw:
        raise ValueError("Config paths must be non-empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _is_reparse(path: Path) -> bool:
    """True for symlinks and Windows junctions, which ``os.walk`` would otherwise enter."""
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def iter_files(folder: Path, name: str | None = None) -> list[Path]:
    """Files under ``folder``, sorted, skipping ``.git`` and not following links.

    ``Path.rglob`` follows directory symlinks on Python 3.11 and 3.12. On Windows,
    ``os.walk(followlinks=False)`` still enters junctions because they are not symlinks.
    """
    if not folder.is_dir():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        kept: list[str] = []
        for dirname in dirnames:
            if dirname == ".git":
                continue
            child = Path(dirpath) / dirname
            if _is_reparse(child):
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for fname in filenames:
            if name is not None and fname != name:
                continue
            path = Path(dirpath) / fname
            if _is_reparse(path) or not path.is_file():
                continue
            found.append(path)
    found.sort()
    return found


def _copy_pack(src: Path, dest: Path) -> None:
    """Copy a pack without following symlinks or Windows junctions."""
    dest.mkdir(parents=True)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        kept: list[str] = []
        for dirname in dirnames:
            if _is_reparse(Path(dirpath) / dirname):
                continue
            kept.append(dirname)
        dirnames[:] = kept
        target_dir = dest / Path(dirpath).relative_to(src)
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            file = Path(dirpath) / fname
            if _is_reparse(file):
                continue
            shutil.copy2(file, target_dir / fname)


def _remove_tree(folder: Path) -> None:
    """Delete a tree without descending into symlinks or Windows junctions."""
    if not folder.exists() and not folder.is_symlink():
        return
    if _is_reparse(folder):
        if folder.is_dir():
            os.rmdir(folder)
        else:
            folder.unlink(missing_ok=True)
        return
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        kept: list[str] = []
        for dirname in dirnames:
            child = Path(dirpath) / dirname
            if _is_reparse(child):
                os.rmdir(child)
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for fname in filenames:
            file = Path(dirpath) / fname
            if _is_reparse(file) or file.is_file() or file.is_symlink():
                file.unlink(missing_ok=True)
    for dirpath, dirnames, _filenames in os.walk(folder, topdown=False, followlinks=False):
        base = Path(dirpath)
        for dirname in dirnames:
            child = base / dirname
            if _is_reparse(child):
                os.rmdir(child)
            elif child.is_dir():
                child.rmdir()
        if base != folder and base.is_dir() and not _is_reparse(base):
            base.rmdir()
    if folder.is_dir() and not _is_reparse(folder):
        folder.rmdir()


def _hooks_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "skill-predator-empty-hooks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    return env


def _run_git(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run git with repository hooks disabled and interactive prompts off."""
    cmd = ["git", "-c", f"core.hooksPath={_hooks_dir().as_posix()}", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"git failed: {exc}") from exc


def folder_sha256(folder: Path) -> str:
    """Compute a deterministic SHA-256 hash of all files under a skill folder."""
    digest = hashlib.sha256()
    for path in iter_files(folder):
        rel = path.relative_to(folder).as_posix().encode("utf-8")
        digest.update(rel + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def parse_skill_md(path: Path) -> dict:
    """Parse name, description, and ## headings from a SKILL.md file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    name = path.parent.name
    description = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            front, body = parts[1], parts[2]
            name, description = _parse_frontmatter(front, default_name=name)
    headings = [m.group(1).strip() for m in _HEADING_RE.finditer(body)]
    return {"name": name, "description": description.strip(), "headings": headings, "path": path}


def _parse_frontmatter(front: str, default_name: str) -> tuple[str, str]:
    name = default_name
    description = ""
    lines = front.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("name:"):
            val = line.split(":", 1)[1].strip().strip("\"'")
            if val:
                name = val
        elif line.startswith("description:"):
            val = line.split(":", 1)[1].strip()
            if val in ("|", ">", "|-", ">-"):
                block = []
                i += 1
                while i < len(lines) and (lines[i].startswith("  ") or lines[i].startswith("\t") or not lines[i].strip()):
                    block.append(lines[i].strip())
                    i += 1
                description = " ".join(b for b in block if b)
                continue
            description = val.strip("\"'")
        i += 1
    return name, description


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _plugin_name(skill_md: Path, plugins_dir: Path) -> str:
    parts = skill_md.relative_to(plugins_dir).parts
    return parts[1] if len(parts) > 1 else parts[0]


def load_lock(cfg: PredatorConfig) -> dict:
    if not cfg.lock_file.is_file():
        return {"skills": {}, "prey": {}}
    return json.loads(cfg.lock_file.read_text(encoding="utf-8"))


def save_lock(cfg: PredatorConfig, lock: dict) -> None:
    cfg.lock_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.lock_file.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


def discover_skills(cfg: PredatorConfig, include_stomach: bool = True) -> list[dict]:
    """Discover SKILL.md files across own, synced, repo, plugin, and optional stomach sources."""
    found: list[dict] = []
    if cfg.skills_dir.is_dir():
        for child in sorted(cfg.skills_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name == "synced":
                for sub in iter_files(child, "SKILL.md"):
                    info = parse_skill_md(sub)
                    info.update(source="synced", editable=False, loads=False)
                    found.append(info)
                continue
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                info = parse_skill_md(skill_md)
                info.update(source="own", editable=True, loads=True)
                found.append(info)

    for scan_root in cfg.scan_roots:
        if not scan_root.is_dir():
            continue
        for repo in sorted(scan_root.iterdir()):
            if not repo.is_dir() or (repo / ".git").is_file():
                continue
            seen_in_repo: set[tuple[str, str]] = set()
            for rel in (".claude/skills", ".agents/skills", "skills"):
                base = repo / rel
                if not base.is_dir():
                    continue
                for skill_md in iter_files(base, "SKILL.md"):
                    info = parse_skill_md(skill_md)
                    key = (info["name"], info["description"])
                    if key in seen_in_repo:
                        continue
                    seen_in_repo.add(key)
                    info.update(source=f"repo:{repo.name}", editable=True, loads=False)
                    found.append(info)

    if cfg.plugins_dir and cfg.plugins_dir.is_dir():
        newest: dict[tuple[str, str], dict] = {}
        for skill_md in iter_files(cfg.plugins_dir, "SKILL.md"):
            pname = _plugin_name(skill_md, cfg.plugins_dir)
            info = parse_skill_md(skill_md)
            info.update(source=f"plugin:{pname}", editable=False, loads=True)
            key = (pname, info["name"])
            old = newest.get(key)
            if old is None or skill_md.stat().st_mtime > old["path"].stat().st_mtime:
                newest[key] = info
        found.extend(newest[k] for k in sorted(newest))

    if include_stomach and cfg.stomach_dir and cfg.stomach_dir.is_dir():
        for pack in sorted(cfg.stomach_dir.iterdir()):
            if not pack.is_dir():
                continue
            seen_in_pack: set[tuple[str, str]] = set()
            for skill_md in iter_files(pack, "SKILL.md"):
                info = parse_skill_md(skill_md)
                key = (info["name"], info["description"])
                if key in seen_in_pack:
                    continue
                seen_in_pack.add(key)
                info.update(source=f"stomach:{pack.name}", editable=False, loads=False)
                found.append(info)

    return found


def find_collisions(skills: list[dict], lock: dict | None = None) -> list[dict]:
    """Find skill pairs whose descriptions overlap past JACCARD_THRESHOLD."""
    copy_paths: set[Path] = set()
    for entry in ((lock or {}).get("skills") or {}).values():
        for c in entry.get("copies") or []:
            copy_paths.add(Path(c["path"]).resolve())

    collisions = []
    words = [_content_words(s["description"]) for s in skills]
    for i in range(len(skills)):
        for j in range(i + 1, len(skills)):
            a, b = skills[i], skills[j]
            if a["path"] == b["path"]:
                continue
            if a["name"] == b["name"] and (
                a["path"].resolve() in copy_paths or b["path"].resolve() in copy_paths
                or (a["source"].startswith("repo:") and b["source"] == a["source"])
            ):
                continue
            if (a["source"].startswith("plugin:") and b["source"].startswith("stomach:")) or (
                b["source"].startswith("plugin:") and a["source"].startswith("stomach:")
            ):
                if a["name"] == b["name"]:
                    continue
            jacc = _jaccard(words[i], words[j])
            if jacc >= JACCARD_THRESHOLD:
                collisions.append({"a": a, "b": b, "jaccard": round(jacc, 2)})
    return collisions


def scan_transcripts(
    projects_dir: Path | None,
    today: str,
    signatures_by_skill: dict[str, list[str]],
    days: int = TRANSCRIPT_DAYS,
) -> tuple[set[str], list[dict]]:
    """Scan Claude Code transcripts for Skill invocations and signature-based silent misses."""
    used: set[str] = set()
    misses: list[dict] = []
    if not projects_dir or not projects_dir.is_dir():
        return used, misses

    cutoff = datetime.combine(date.fromisoformat(today) - timedelta(days=days), datetime.min.time())
    compiled = {
        skill: [re.compile(pat, re.IGNORECASE) for pat in pats]
        for skill, pats in signatures_by_skill.items()
        if pats
    }

    for jsonl in sorted(projects_dir.rglob("*.jsonl")):
        if "subagents" in jsonl.parts:
            continue
        try:
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            continue

        session_skills: set[str] = set()
        command_texts: list[str] = []
        session_date = mtime.date().isoformat()
        repo_name = jsonl.parent.name
        session_id = jsonl.stem

        for raw in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool_use":
                    continue
                tname = part.get("name", "")
                tinp = part.get("input") or {}
                if tname == "Skill":
                    sname = str(tinp.get("skill", "")).split(":")[-1].strip()
                    if sname:
                        session_skills.add(sname)
                        used.add(sname)
                elif compiled and tname == "Bash":
                    command_texts.append(str(tinp.get("command", "")))
                elif compiled and tname.startswith("mcp__"):
                    command_texts.append(tname + " " + json.dumps(tinp, ensure_ascii=False))

        if compiled and command_texts:
            blob = "\n".join(command_texts)
            for skill, regexes in compiled.items():
                if skill in session_skills:
                    continue
                for rx in regexes:
                    if rx.search(blob):
                        misses.append({
                            "date": session_date,
                            "repo": repo_name,
                            "session_id": session_id,
                            "skill": skill,
                            "pattern": rx.pattern,
                        })
                        break

    return used, misses


def apply_transform(text: str, transform: str, replacements: dict[str, str] | None = None) -> str:
    """Apply a named export transform (verbatim, strip-footer, or custom replacements)."""
    norm = text.replace("\r\n", "\n")
    if transform == "verbatim":
        return norm.rstrip() + "\n"
    if transform == "strip-footer":
        out = _FOOTER_RE.sub("", norm)
        return out.rstrip() + "\n"
    if transform == "replace":
        out = _FOOTER_RE.sub("", norm)
        for old, new in (replacements or {}).items():
            out = out.replace(old, new)
        return out.rstrip() + "\n"
    raise ValueError(f"Unknown copy transform: {transform}")


def check_copy_drift(cfg: PredatorConfig, lock: dict) -> list[dict]:
    """Compare each locked copy against its transformed source."""
    results = []
    for name, entry in sorted((lock.get("skills") or {}).items()):
        source_md = cfg.skills_dir / name / "SKILL.md"
        if not source_md.is_file():
            continue
        source_text = source_md.read_text(encoding="utf-8", errors="replace")
        source_mtime = source_md.stat().st_mtime
        for copy in entry.get("copies") or []:
            copy_path = Path(copy["path"])
            transform = copy.get("transform", "verbatim")
            if not copy_path.is_file():
                results.append({"skill": name, "path": str(copy_path), "status": "missing", "newer": "n/a", "delta": ""})
                continue
            try:
                expected = apply_transform(source_text, transform, copy.get("replacements"))
            except ValueError as exc:
                results.append({"skill": name, "path": str(copy_path), "status": "error", "newer": "n/a", "delta": str(exc)})
                continue
            actual = copy_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").rstrip() + "\n"
            if expected == actual:
                continue
            copy_mtime = copy_path.stat().st_mtime
            newer = "copy newer" if copy_mtime > source_mtime else ("source newer" if source_mtime > copy_mtime else "same mtime")
            diff = list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), lineterm=""))
            added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            results.append({"skill": name, "path": str(copy_path), "status": "drifted", "newer": newer, "delta": f"+{added}/-{removed}"})
    return results


def write_hunt(cfg: PredatorConfig, today: str) -> Path:
    """Run deterministic territory hunt and write YYYY-MM-DD-hunt.md."""
    lock = load_lock(cfg)
    skills = discover_skills(cfg, include_stomach=True)
    collisions = find_collisions(skills, lock=lock)
    signatures = {name: entry.get("signatures") or [] for name, entry in (lock.get("skills") or {}).items()}
    used, misses = scan_transcripts(cfg.projects_dir, today, signatures)

    loaded = [s for s in skills if s["loads"]]
    never_used = [s for s in loaded if s["name"] not in used]
    total_chars = sum(len(s["description"]) for s in loaded)
    longest = sorted(loaded, key=lambda s: len(s["description"]), reverse=True)[:10]
    over_limit = [s for s in loaded if len(s["description"]) > MAX_DESCRIPTION_CHARS]

    locked_skills = lock.get("skills") or {}
    unknown_origin = []
    for s in skills:
        if s["source"] != "own":
            continue
        entry = locked_skills.get(s["name"])
        if not entry:
            unknown_origin.append((s["name"], "missing from sources.json"))
        elif "<owner>" in str(entry.get("upstream", "")) or entry.get("origin") == "unknown":
            unknown_origin.append((s["name"], f"placeholder upstream ({entry.get('upstream')})"))

    drifted = check_copy_drift(cfg, lock)

    lines = [
        f"# Skill Hunt — {today}",
        "",
        f"Scanned {len(skills)} skills ({len(loaded)} loaded into sessions).",
        "",
        "## Collisions",
        "",
    ]
    if collisions:
        for c in collisions:
            a, b = c["a"], c["b"]
            note = " — consider disabling plugin in `~/.claude/settings.json` (`enabledPlugins`)" if (
                a["source"].startswith("plugin:") or b["source"].startswith("plugin:")
            ) else ""
            lines.append(
                f"- **{a['name']}** (`{a['source']}`, editable={a['editable']}) vs "
                f"**{b['name']}** (`{b['source']}`, editable={b['editable']}) — Jaccard {c['jaccard']}{note}"
            )
    else:
        lines.append("- None found.")

    lines += ["", "## Never used (last 30 days, Claude Code `Skill` calls)", "", "_Note: `read_file`-based skill loads in other harnesses are not counted here._", ""]
    lines.extend([f"- `{s['name']}` ({s['source']})" for s in never_used] or ["- None."])
    lines += ["", "## Silent misses", ""]
    lines.extend([f"- {m['date']} `{m['repo']}` `{m['session_id']}`: `{m['skill']}` (matched `{m['pattern']}`)" for m in misses] or ["- None detected from locked `signatures`."])
    lines += ["", "## Budget", "", f"- Total loaded description size: {total_chars:,} chars across {len(loaded)} skills.", f"- Over {MAX_DESCRIPTION_CHARS} chars: {len(over_limit)}"]
    lines.extend([f"  - `{s['name']}` ({s['source']}): {len(s['description'])} chars" for s in over_limit])
    lines.append("- 10 longest:")
    lines.extend([f"  - `{s['name']}` ({s['source']}): {len(s['description'])} chars" for s in longest])
    lines += ["", "## Unknown origin", ""]
    lines.extend([f"- `{n}`: {r}" for n, r in unknown_origin] or ["- None."])
    lines += ["", "## Drifted copies", ""]
    lines.extend([f"- `{d['skill']}` -> `{d['path']}`: {d['status']} ({d['newer']}, {d['delta']})" for d in drifted] or ["- None."])

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.out_dir / f"{today}-hunt.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def isolate_skill_folder(folder: Path) -> dict:
    """List scripts, hooks, installers, dependency manifests, and network calls without running them."""
    scripts: list[str] = []
    hooks_or_installers: list[str] = []
    dependencies: list[str] = []
    network_calls: list[str] = []

    for path in iter_files(folder):
        rel = path.relative_to(folder).as_posix()
        if path.suffix.lower() in SCRIPT_EXTS:
            scripts.append(rel)
        if _HOOK_NAME_RE.search(path.name):
            hooks_or_installers.append(rel)
        if path.name in DEPENDENCY_FILES:
            dependencies.append(rel)
        if path.suffix.lower() in SCRIPT_EXTS or path.name in DEPENDENCY_FILES or path.name == "SKILL.md":
            if _NET_CALL_RE.search(path.read_text(encoding="utf-8", errors="replace")):
                network_calls.append(rel)

    return {
        "isolated": bool(scripts or hooks_or_installers or dependencies or network_calls),
        "scripts": scripts,
        "hooks_or_installers": hooks_or_installers,
        "dependencies": dependencies,
        "network_calls": network_calls,
    }


def detect_pack_licence(pack_dir: Path) -> str:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        p = pack_dir / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "MIT License" in text or "Permission is hereby granted, free of charge" in text:
            return "MIT"
        if "Apache License" in text and "Version 2.0" in text:
            return "Apache-2.0"
        if "CC-BY-SA" in text or "Attribution-ShareAlike" in text:
            return "CC-BY-SA-4.0"
        if "Creative Commons Attribution 4.0" in text or "CC-BY-4.0" in text or "CC BY 4.0" in text:
            return "CC-BY-4.0"
        if "GNU GENERAL PUBLIC LICENSE" in text:
            return "GPL"
        return "custom"
    return "unknown"


def _git_head_commit(folder: Path) -> str:
    if not (folder / ".git").exists():
        return ""
    try:
        res = _run_git(
            ["--git-dir", str(folder / ".git"), "-C", str(folder), "rev-parse", "--short", "HEAD"],
            timeout=15,
        )
    except RuntimeError:
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _pack_name_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def devour_packs(cfg: PredatorConfig, today: str, url: str | None = None, path: Path | None = None) -> Path:
    """Devour a pack (or all packs already in stomach_dir), run isolate, update prey lock, and write report."""
    cfg.stomach_dir.mkdir(parents=True, exist_ok=True)
    targets: list[Path] = []
    if url:
        dest = cfg.stomach_dir / _pack_name_from_url(url)
        if not dest.exists():
            res = _run_git(["clone", "--", url, str(dest)], timeout=60)
            if res.returncode != 0:
                detail = (res.stderr or res.stdout).strip() or str(res.returncode)
                raise RuntimeError(f"git clone failed for {url}: {detail}")
        targets.append(dest)
    elif path:
        src = path.resolve()
        if not src.is_dir():
            raise ValueError(f"Pack folder does not exist: {path}")
        if src.parent.resolve() == cfg.stomach_dir.resolve():
            targets.append(src)
        else:
            dest = cfg.stomach_dir / src.name
            if dest.exists():
                _remove_tree(dest)
            _copy_pack(src, dest)
            targets.append(dest)
    else:
        targets = [p for p in sorted(cfg.stomach_dir.iterdir()) if p.is_dir()]

    lock = load_lock(cfg)
    prey_lock = lock.setdefault("prey", {})
    lines = [f"# Skill Devour & Isolate — {today}", ""]
    total_skills = 0
    total_isolated = 0

    for pack_dir in targets:
        pack_name = pack_dir.name
        commit = _git_head_commit(pack_dir)
        licence = detect_pack_licence(pack_dir)
        lines.extend([f"## {pack_name}", "", f"- Commit: `{commit or 'none'}` | Licence: `{licence}`", ""])
        seen: set[tuple[str, str]] = set()
        for skill_md in iter_files(pack_dir, "SKILL.md"):
            info = parse_skill_md(skill_md)
            dedup_key = (info["name"], info["description"])
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            total_skills += 1
            iso = isolate_skill_folder(skill_md.parent)
            if iso["isolated"]:
                total_isolated += 1
            prey_key = f"{pack_name}/{info['name']}"
            existing = prey_lock.get(prey_key, {})
            preserved = existing.get("state")
            state = preserved if preserved in ("mimicked", "skipped") else ("isolated" if iso["isolated"] else "devoured")
            prey_lock[prey_key] = {
                **existing,
                "state": state,
                "commit": commit,
                "licence": licence,
                "isolated": iso["isolated"],
                "sha256": folder_sha256(skill_md.parent),
                "sealed": {k: v for k, v in iso.items() if k != "isolated" and v},
            }
            lines.append(f"- `{info['name']}`: **{'ISOLATED' if iso['isolated'] else 'clean'}** — lock state `{state}`")
        lines.append("")

    save_lock(cfg, lock)
    lines.insert(2, f"Devoured {len(targets)} pack(s), {total_skills} skill(s) ({total_isolated} isolated).")
    lines.insert(3, "")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.out_dir / f"{today}-devour.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def write_analyse(cfg: PredatorConfig, today: str, pack: str | None = None) -> Path:
    """Compare devoured stomach skills against territory rivals and propose initial verdicts."""
    lock = load_lock(cfg)
    prey_lock = lock.get("prey") or {}
    territory = discover_skills(cfg, include_stomach=False)
    stomach = [s for s in discover_skills(cfg, include_stomach=True) if s["source"].startswith("stomach:")]
    if pack:
        stomach = [s for s in stomach if s["source"] == f"stomach:{pack}"]

    t_words = [_content_words(t["description"]) for t in territory]
    lines = [f"# Skill Analysis — {today}", ""]
    for prey in stomach:
        pack_name = prey["source"].split(":", 1)[1]
        prey_key = f"{pack_name}/{prey['name']}"
        meta = prey_lock.get(prey_key, {})
        licence = meta.get("licence", "unknown")
        isolated = meta.get("isolated", False)
        state = meta.get("state", "devoured")

        p_words = _content_words(prey["description"])
        scored = sorted(
            ((_jaccard(p_words, t_words[i]), t) for i, t in enumerate(territory) if _jaccard(p_words, t_words[i]) > 0),
            key=lambda item: item[0],
            reverse=True,
        )[:3]
        best_jacc, best_rival = scored[0] if scored else (0.0, None)
        copyleft = licence in ("GPL", "CC-BY-SA-4.0")

        if state in ("skipped", "mimicked"):
            verdict = f"Already `{state}`"
        elif best_rival and best_jacc >= 0.8:
            verdict = f"**Skip** (covered by `{best_rival['name']}` at Jaccard {best_jacc:.2f})"
        elif best_rival and best_jacc >= JACCARD_THRESHOLD:
            verdict = (
                f"**Review (New-only / Skip)** — overlaps `{best_rival['name']}`, but `{licence}` blocks mimic"
                if copyleft else f"**Mimic / Evolve** into `{best_rival['name']}` ({best_rival['source']}, Jaccard {best_jacc:.2f})"
            )
        else:
            verdict = "**Review: New vs Skip** (no strong territory rival)"

        iso_note = " | **ISOLATED: read sealed files before promoting**" if isolated else ""
        lines.extend([
            f"### `{prey_key}`",
            "",
            f"- Verdict: {verdict}{iso_note}",
            f"- Licence: `{licence}` | Lock state: `{state}`",
            f"- Nearest rivals: {', '.join(f'`{t['name']}` ({t['source']}, J={j:.2f})' for j, t in scored) or 'none'}",
            "",
        ])

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.out_dir / f"{today}-analyse.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _score_query_match(query_words: set[str], name: str, description: str) -> tuple[float, list[str]]:
    text_words = _content_words(f"{name} {description}")
    overlap = sorted(query_words & text_words)
    if not overlap:
        return 0.0, []
    jacc = len(overlap) / len(query_words | text_words)
    name_hits = sum(1 for w in overlap if w in name.lower())
    return round(jacc + 0.15 * name_hits, 3), overlap


def _licence_label(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("key") or value.get("name") or "unknown")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "unknown"


def search_github_skills(query: str, limit: int = 5) -> tuple[list[dict], str | None]:
    cmd = [
        "gh", "search", "repos",
        "--json", "fullName,description,stargazersCount,updatedAt,license,url",
        "--limit", str(limit),
        "--", f"{query} agent skill",
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"gh search unavailable: {exc}"
    if res.returncode != 0:
        return [], f"gh search failed: {(res.stderr or '').strip() or res.returncode}"
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return [], "gh search returned invalid JSON"
    if not isinstance(rows, list):
        return [], "gh search returned unexpected JSON"
    hits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        hits.append({
            "name": row.get("fullName", "unknown"),
            "source": "github",
            "stars": row.get("stargazersCount", 0),
            "updated": str(row.get("updatedAt") or "")[:10] or "unknown",
            "licence": _licence_label(row.get("license")),
            "why": str(row.get("description") or "").strip()[:140] or "matched GitHub search",
            "url": row.get("url", ""),
            "score": 0.2,
        })
    return hits, None


def search_skills_sh(query: str, limit: int = 5) -> tuple[list[dict], str | None]:
    url = f"https://skills.sh/api/search?q={urllib.parse.quote_plus(query)}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "skill-predator/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return [], f"skills.sh unavailable: {exc}"
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("skills") or payload.get("results") or []
    else:
        return [], "skills.sh returned unexpected JSON"
    if not isinstance(items, list):
        return [], "skills.sh returned unexpected JSON"
    return [
        {
            "name": item.get("name") or item.get("id") or "unknown",
            "source": f"skills.sh:{item.get('source') or item.get('repo') or 'skills.sh'}",
            "stars": item.get("installs") or item.get("stars") or "n/a",
            "updated": str(item.get("updatedAt") or "unknown")[:10],
            "licence": item.get("licence") or item.get("license") or "unknown",
            "why": str(item.get("description") or "matched skills.sh index")[:140],
            "url": item.get("url") or "",
            "score": 0.2,
        }
        for item in items[:limit] if isinstance(item, dict)
    ], None


def write_hunt_for(cfg: PredatorConfig, today: str, job: str, include_public: bool = True) -> Path:
    """Outside hunt: search stomach, territory, marketplaces, GitHub, and skills.sh for a job."""
    q_words = _content_words(job)
    lock = load_lock(cfg)
    prey_lock = lock.get("prey") or {}
    skills_lock = lock.get("skills") or {}
    candidates: list[dict] = []
    notes: list[str] = []

    for s in discover_skills(cfg, include_stomach=True):
        score, overlap = _score_query_match(q_words, s["name"], s["description"])
        if score <= 0:
            continue
        if s["source"] == "own":
            licence = (skills_lock.get(s["name"]) or {}).get("licence", "own")
            bonus = 1.0
        elif s["source"].startswith("stomach:"):
            pack = s["source"].split(":", 1)[1]
            licence = (prey_lock.get(f"{pack}/{s['name']}") or {}).get("licence", "unknown")
            bonus = 0.8
        else:
            licence = "unknown"
            bonus = 0.7
        candidates.append({
            "name": s["name"], "source": s["source"], "stars": "local", "updated": "local",
            "licence": licence, "why": f"matches {', '.join(overlap)}: {s['description'][:120]}",
            "url": str(s["path"].parent), "score": score + bonus,
        })

    if cfg.marketplaces_dir and cfg.marketplaces_dir.is_dir():
        for mkt in sorted(cfg.marketplaces_dir.iterdir()):
            if not mkt.is_dir() or mkt.name.endswith("..clone"):
                continue
            for skill_md in iter_files(mkt, "SKILL.md"):
                info = parse_skill_md(skill_md)
                score, overlap = _score_query_match(q_words, info["name"], info["description"])
                if score > 0:
                    candidates.append({
                        "name": info["name"], "source": f"marketplace:{mkt.name}", "stars": "n/a", "updated": "local",
                        "licence": "see marketplace", "why": f"matches {', '.join(overlap)}: {info['description'][:120]}",
                        "url": str(skill_md.parent), "score": score + 0.5,
                    })

    if include_public:
        for hits, note in (search_github_skills(job, 5), search_skills_sh(job, 5)):
            if note:
                notes.append(note)
            candidates.extend(hits)

    dedup: dict[tuple[str, str], dict] = {}
    for c in candidates:
        k = (c["name"], c["source"])
        if k not in dedup or c["score"] > dedup[k]["score"]:
            dedup[k] = c
    top = sorted(dedup.values(), key=lambda c: c["score"], reverse=True)[:5]

    lines = [f"# Outside Skill Hunt — {today}", "", f"Query: `{job}`", "", "## Candidates (up to 5)", ""]
    if top:
        for idx, c in enumerate(top, 1):
            lines.append(f"{idx}. **{c['name']}** (`{c['source']}`) — stars: {c['stars']}, updated: {c['updated']}, licence: `{c['licence']}`")
            lines.append(f"   - Why it might fit: {c['why']}")
            if c.get("url"):
                lines.append(f"   - Location: `{c['url']}`")
    else:
        lines.append("- No candidate found — valid result: write the new skill.")
    if notes:
        lines += ["", "## Search notes", ""] + [f"- {n}" for n in notes]

    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.out_dir / f"{today}-hunt-for.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def three_way_merge(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Run a deterministic 3-way merge via `git merge-file`. Returns (merged_text, has_conflict)."""
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        p_base, p_ours, p_theirs = td / "base.md", td / "ours.md", td / "theirs.md"
        p_base.write_text(base.replace("\r\n", "\n"), encoding="utf-8")
        p_ours.write_text(ours.replace("\r\n", "\n"), encoding="utf-8")
        p_theirs.write_text(theirs.replace("\r\n", "\n"), encoding="utf-8")
        try:
            res = subprocess.run(
                ["git", "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "upstream", str(p_ours), str(p_base), str(p_theirs)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=15,
                env=_git_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"git merge-file failed: {exc}") from exc
        if res.returncode < 0:
            raise RuntimeError(f"git merge-file failed: {(res.stderr or '').strip()}")
        return res.stdout, res.returncode > 0


def write_update(cfg: PredatorConfig, today: str, fetch: bool = False) -> Path:
    """Check adopted skills against stomach/upstream and copies for drift; propose 3-way merges."""
    lock = load_lock(cfg)
    fetch_notes: list[str] = []
    if fetch and cfg.stomach_dir.is_dir():
        for pack in sorted(p for p in cfg.stomach_dir.iterdir() if p.is_dir()):
            if not (pack / ".git").exists():
                continue
            try:
                res = _run_git(
                    ["--git-dir", str(pack / ".git"), "-C", str(pack), "pull", "--ff-only"],
                    timeout=30,
                )
            except RuntimeError as exc:
                fetch_notes.append(f"- `{pack.name}`: fetch failed ({exc})")
                continue
            if res.returncode != 0:
                detail = (res.stderr or res.stdout or "").strip() or str(res.returncode)
                fetch_notes.append(f"- `{pack.name}`: fetch failed ({detail})")

    upstream_lines: list[str] = []
    for name, entry in sorted((lock.get("skills") or {}).items()):
        if entry.get("origin") != "adopted":
            continue
        local_md = cfg.skills_dir / name / "SKILL.md"
        if not local_md.is_file():
            continue
        upstream_folder = None
        if cfg.stomach_dir.is_dir():
            for pack in sorted(p for p in cfg.stomach_dir.iterdir() if p.is_dir()):
                for sm in iter_files(pack, "SKILL.md"):
                    if sm.parent.name == name:
                        upstream_folder = sm.parent
                        break
                if upstream_folder:
                    break
        if not upstream_folder:
            upstream_lines.append(f"- `{name}`: no stomach upstream folder found (`{entry.get('upstream', 'unknown')}`)")
            continue
        up_sha = folder_sha256(upstream_folder)
        if up_sha == (entry.get("base_sha256") or entry.get("sha256") or ""):
            upstream_lines.append(f"- `{name}`: up to date (`{up_sha[:12]}`)")
            continue
        ours_text = local_md.read_text(encoding="utf-8", errors="replace")
        theirs_text = (upstream_folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        base_text = entry.get("base_text")
        if base_text is None:
            upstream_lines.append(f"- `{name}`: **upstream changed** (no recorded base text)")
            continue
        try:
            merged, conflict = three_way_merge(base_text, ours_text, theirs_text)
        except RuntimeError as exc:
            upstream_lines.append(f"- `{name}`: merge failed ({exc})")
            continue
        if conflict:
            upstream_lines.append(f"- `{name}`: **CONFLICT** on 3-way merge")
            upstream_lines.extend(["```markdown", *merged.splitlines()[:80], "```"])
        else:
            diff = list(difflib.unified_diff(ours_text.splitlines(), merged.splitlines(), lineterm=""))
            if not diff:
                upstream_lines.append(f"- `{name}`: upstream changed, local already matches.")
            else:
                upstream_lines.append(f"- `{name}`: **clean 3-way merge proposed**:")
                upstream_lines.extend(["```diff", *diff[:60], "```"])

    copy_lines = [
        f"- `{d['skill']}` -> `{d['path']}`: {d['status']} ({d['newer']}, {d['delta']})"
        for d in check_copy_drift(cfg, lock)
    ]
    lines = [f"# Skill Update — {today}", ""]
    if fetch_notes:
        lines.extend(["## Fetch", "", *fetch_notes, ""])
    lines.extend([
        "## Upstream",
        "",
        *(upstream_lines or ["- No adopted skills with upstream changes."]),
        "",
        "## Copies",
        "",
        *(copy_lines or ["- No drifted copies."]),
        "",
    ])
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = cfg.out_dir / f"{today}-update.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def sort_merge_files(ours_dir: Path, theirs_dir: Path) -> dict[str, list[str]]:
    if not ours_dir.is_dir():
        raise ValueError(f"Skill folder does not exist: {ours_dir}")
    if not theirs_dir.is_dir():
        raise ValueError(f"Skill folder does not exist: {theirs_dir}")
    ours_files = {p.relative_to(ours_dir).as_posix(): p for p in iter_files(ours_dir)}
    theirs_files = {p.relative_to(theirs_dir).as_posix(): p for p in iter_files(theirs_dir)}
    redundant, comp_ours, comp_theirs, conflicting = [], [], [], []
    for rel in sorted(set(ours_files) | set(theirs_files)):
        if rel in ours_files and rel not in theirs_files:
            comp_ours.append(rel)
        elif rel in theirs_files and rel not in ours_files:
            comp_theirs.append(rel)
        elif ours_files[rel].read_bytes() == theirs_files[rel].read_bytes():
            redundant.append(rel)
        else:
            conflicting.append(rel)
    return {"redundant": redundant, "complementary_ours": comp_ours, "complementary_theirs": comp_theirs, "conflicting": conflicting}


def excrete_skill(cfg: PredatorConfig, today: str, skill_name: str, winner: str, apply: bool = False) -> Path:
    """Archive a skill. Dry-run returns the planned archive path and moves nothing."""
    if (
        not skill_name
        or skill_name in {".", ".."}
        or "/" in skill_name
        or "\\" in skill_name
        or skill_name != Path(skill_name).name
    ):
        raise ValueError(f"Skill name must be a single folder under skills/: {skill_name}")
    src = cfg.skills_dir / skill_name
    if not src.is_dir() or src.resolve().parent != cfg.skills_dir.resolve():
        raise ValueError(f"Skill folder does not exist: {src}")
    dest = cfg.archive_dir / skill_name
    if dest.exists():
        raise ValueError(f"Archive destination already exists: {dest}")
    if not apply:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    (dest / "POINTER.md").write_text(f"# Excreted: {skill_name}\n\n- Date: {today}\n- Replaced by: `{winner}`\n", encoding="utf-8")
    lock = load_lock(cfg)
    entry = lock.setdefault("skills", {}).setdefault(skill_name, {})
    entry["origin"] = "excreted"
    entry["excreted_at"] = today
    entry["winner"] = winner
    save_lock(cfg, lock)
    return dest
