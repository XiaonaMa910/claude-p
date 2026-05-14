# claude-p

`claude-p` is a `claude -p` compatible Python CLI and SDK backed by the
interactive Claude Code TUI.

Use it when `claude -p` is unavailable in an environment, but interactive
`claude` works with the local Claude Code subscription login state.

## Install

```bash
pip install claude-p
```

This installs global console scripts into the active Python environment:

```bash
claude-p "Respond exactly: hello"
claude-p.py "Respond exactly: hello"
```

If your shell cannot find `claude-p` after installation, your Python scripts
directory is not on `PATH`. Check it with:

```bash
python -m site --user-base
python -m pip show -f claude-p
```

For a virtualenv/conda environment, activate the environment first and prefer:

```bash
python -m pip install claude-p
python -m pip show -f claude-p
which claude-p
```

## CLI

Default output matches `claude -p`: plain text.

```bash
claude-p "Respond exactly: hello"
```

Structured outputs:

```bash
claude-p "Respond exactly: hello" --output-format json
claude-p "Respond exactly: hello" --output-format stream-json --include-partial-messages
```

The CLI accepts a broad subset of `claude -p` flags, including:

- `-p`, `--print`
- `--model`
- `--tools`
- `--permission-mode`
- `--output-format text|json|stream-json`
- `--include-partial-messages`
- `--session-id`
- `--cwd`
- common Claude Code context/config flags such as `--system-prompt`,
  `--append-system-prompt`, `--mcp-config`, `--settings`, `--plugin-dir`,
  `--allowedTools`, `--disallowedTools`, `--resume`, and `--continue`

Known limits:

- Token usage, cost, and exact rate-limit fields are best-effort placeholders.
- Hook lifecycle events from `claude -p --include-hook-events` are not replayed yet.
- `--input-format stream-json` is accepted but not implemented.
- `--bare` conflicts with the subscription-login goal because Claude bare mode
  bypasses OAuth/keychain auth.

## Python SDK

The API is intentionally shaped like the official Claude Agent SDK:

```python
import asyncio
from claude_p import ClaudePOptions, query


async def main():
    options = ClaudePOptions(
        model="sonnet",
        tools="default",
        permission_mode="default",
    )

    async for message in query("这个目录里有多少个文件", options=options):
        print(message)


asyncio.run(main())
```

For a single final result:

```python
import asyncio
from claude_p import ClaudePClient, ClaudePOptions


async def main():
    async with ClaudePClient(ClaudePOptions(model="sonnet")) as client:
        result = await client.run("Respond exactly: SDK_OK")
        print(result.result)


asyncio.run(main())
```

## How it works

The wrapper does not call `claude -p`.

It:

1. starts interactive `claude` in a pseudo-TTY;
2. passes a deterministic `--session-id`;
3. waits for the TUI response to complete;
4. reads Claude Code's canonical session JSONL from
   `~/.claude/projects/**/<session-id>.jsonl`;
5. emits text/json/stream-json output compatible with `claude -p`.

The session JSONL is required because terminal rendering is lossy and can drop
characters during redraws.

## Development and Release Flow

Branching:

- `main`: released versions only.
- `dev`: integration branch.
- `feat/<name>`: feature branches opened from `dev`.

Local validation:

```bash
uv run --with pytest pytest tests -q
uv build
uvx twine check dist/*
python -m venv /tmp/claude-p-smoke
/tmp/claude-p-smoke/bin/python -m pip install dist/*.whl
/tmp/claude-p-smoke/bin/claude-p "Respond exactly: SMOKE_OK" --tools ''
```

Release:

1. Merge feature branch into `dev`.
2. Open PR from `dev` to `main`.
3. After merge, create a GitHub release tag, for example `v0.1.1`.
4. The `publish.yml` workflow publishes to PyPI when PyPI Trusted Publishing is configured.
