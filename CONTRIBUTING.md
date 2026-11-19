# Contributing to skill-predator

PRs and issues are welcome.

## Ground rules

- **Zero runtime dependencies:** `skill_predator` uses only the Python 3.11+ standard library and `git` (plus optional `gh` for GitHub search).
- **Fierce in the stomach, careful in the body:** `hunt`, `devour`, `isolate`, `analyse`, `evolve`, and `update` must never modify live `skills/` or execute scripts, hooks, or installers from devoured packs.
- **No private paths:** Keep defaults cross-platform (`~`, relative workspace paths, or `skill-predator.json`).

## Development

```bash
pip install -e .
python -m pytest -v
```

Add or update tests in `tests/test_skill_predator.py` for any new command, transform, or edge case before opening a PR.
