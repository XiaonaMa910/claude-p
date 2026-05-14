# Development and Release Process

## Branches

- `main` is release-only.
- `dev` is the integration branch.
- Feature work starts from `dev` and uses `feat/<short-name>`.

## Local Setup

```bash
git clone https://github.com/Equality-Machine/claude-p.git
cd claude-p
uv run --with pytest pytest tests -q
```

## Global CLI Smoke Test

The package exposes two console scripts:

- `claude-p`
- `claude-p.py`

Validate them from a clean environment:

```bash
uv build
python -m venv /tmp/claude-p-smoke
/tmp/claude-p-smoke/bin/python -m pip install dist/*.whl
/tmp/claude-p-smoke/bin/claude-p "Respond exactly: SMOKE_OK" --tools ''
/tmp/claude-p-smoke/bin/claude-p.py "Respond exactly: SMOKE_PY_OK" --tools ''
```

## Release

1. Merge `feat/<name>` into `dev`.
2. Open a PR from `dev` to `main`.
3. Merge only after local validation passes.
4. Create a GitHub release tag.
5. PyPI publishing runs from `.github/workflows/publish.yml`.

PyPI should use Trusted Publishing:

```text
Project: claude-p
Owner: Equality-Machine
Repository: claude-p
Workflow: publish.yml
Environment: <empty>
```

