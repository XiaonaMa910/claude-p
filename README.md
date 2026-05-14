# claude-p — bring `claude -p` back to subscription users

> Use what you already paid for: `claude -p`-style automation on top of your
> interactive Claude Code subscription session.

[![PyPI](https://img.shields.io/pypi/v/claude-p.svg)](https://pypi.org/project/claude-p/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/Equality-Machine/claude-p)](https://github.com/Equality-Machine/claude-p/releases)

`claude-p` is a `claude -p` compatible CLI and Python SDK backed by the
interactive Claude Code TUI.

It is built for the annoying gap where interactive Claude Code works with your
subscription login, but `claude -p` / programmatic agent workflows are limited,
capped, or unavailable in the environment you are using.

## Why claude-p

Claude Code users increasingly rely on `claude -p` for scripts, agent harnesses,
local evals, CI-like workflows, and one-off automations. But in some setups,
programmatic `claude -p` usage does not behave like the interactive subscription
session you already pay for.

`claude-p` bridges that gap.

It exposes the **interactive** Claude Code TUI through a `claude -p`-compatible
CLI and an Agent-SDK-shaped Python API:

- no Anthropic API key required;
- no new account;
- no new subscription;
- same local Claude Code login state;
- familiar `claude -p` output formats: `text`, `json`, and `stream-json`.

Under the hood, `claude-p` starts interactive `claude` in a pseudo-TTY, assigns a
session id, waits for the response, then reads Claude Code's canonical session
JSONL so the final output is not dependent on lossy terminal scraping.

## 30-second quickstart

```bash
pip install claude-p
claude-p "Write a Python function that reverses a string"
```

![claude-p CLI demo](assets/claude-p-cli.gif)

Default output is plain text, just like `claude -p`.

Structured outputs work too:

```bash
claude-p "Respond exactly: hello" --output-format json
claude-p "Respond exactly: hello" --output-format stream-json --include-partial-messages
```

## Drop-in compatibility

| If you use | Try |
|---|---|
| `claude -p "prompt"` | `claude-p "prompt"` |
| `claude -p --output-format json "prompt"` | `claude-p --output-format json "prompt"` |
| `claude -p --output-format stream-json` | `claude-p --output-format stream-json` |
| `claude -p --model sonnet` | `claude-p --model sonnet` |
| `claude -p --tools ''` | `claude-p --tools ''` |
| `claude -p --permission-mode default` | `claude-p --permission-mode default` |
| `from claude_agent_sdk import query` | `from claude_p import query` |

`claude-p` accepts a broad subset of Claude Code CLI flags, including:

- `-p`, `--print`
- `--model`
- `--tools`
- `--permission-mode`
- `--output-format text|json|stream-json`
- `--include-partial-messages`
- `--session-id`
- `--cwd`
- common context/config flags such as `--system-prompt`,
  `--append-system-prompt`, `--mcp-config`, `--settings`, `--plugin-dir`,
  `--allowedTools`, `--disallowedTools`, `--resume`, and `--continue`

## Python SDK

The API is intentionally shaped like the official Claude Agent SDK.

```python
import asyncio
from claude_p import ClaudePOptions, query


async def main():
    options = ClaudePOptions(
        model="sonnet",
        tools="default",
        permission_mode="default",
    )

    async for message in query("How many files are in this directory?", options=options):
        print(message)


asyncio.run(main())
```

For a single final result:

![claude-p Python SDK demo](assets/claude-p-sdk.gif)

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

`claude-p` does **not** call `claude -p`.

It:

1. starts interactive `claude` in a pseudo-TTY;
2. passes a deterministic `--session-id`;
3. waits for the TUI response to complete;
4. reads Claude Code's canonical session JSONL from
   `~/.claude/projects/**/<session-id>.jsonl`;
5. emits text/json/stream-json output compatible with `claude -p`.

The session JSONL step matters. Terminal rendering is lossy: cursor redraws,
spinners, and wide glyphs can drop characters in captured TTY output. The JSONL
session record is the stable source of truth for final `assistant` and `result`
text.

## What works today

- Plain text output by default.
- JSON single-result output.
- Core `stream-json` event shape.
- Tool use through the interactive Claude Code session.
- Python SDK with `query(...)` and `ClaudePClient(...).run(...)`.
- Global console command after `pip install claude-p`.

## Known limits

`claude-p` is a developer convenience tool, not a replacement for the official
Anthropic API.

Current limits:

- Token usage, cost, and exact rate-limit fields are best-effort placeholders.
- Hook lifecycle events from `claude -p --include-hook-events` are not replayed yet.
- `--input-format stream-json` is accepted but not implemented.
- `--bare` conflicts with the subscription-login goal because Claude bare mode
  bypasses OAuth/keychain auth.
- TUI automation can break if Claude Code significantly changes its interactive
  interface or local session storage.

For production agent workloads, use the official Anthropic API / official Claude
Agent SDK when possible.

## FAQ

### Is this bypassing authentication?

No. `claude-p` uses your local authenticated Claude Code session. If interactive
Claude Code does not work on your machine, `claude-p` will not magically make it
work.

### Is this avoiding billing or rate limits?

No. It does not remove Anthropic-side rate limits or account policy. It exposes
your interactive Claude Code session through a programmatic interface. Any limits
that apply to your account still apply.

### Why not just use `claude -p`?

If `claude -p` works for your use case, keep using it. `claude-p` is for
environments where interactive Claude Code works with subscription login state,
but `claude -p` / programmatic usage is unavailable or hits a different limit
surface first.

### Why read session JSONL instead of terminal text?

Because terminal capture is not reliable enough for exact agent output. In local
testing, the rendered terminal surface dropped characters during redraws, while
Claude Code's session JSONL contained the exact final assistant message.

### Will this break?

Possibly. It depends on interactive Claude Code behavior and local session JSONL
format. The project is intentionally small so breakages can be fixed quickly.

## Installation troubleshooting

If your shell cannot find `claude-p` after installation, your Python scripts
directory is not on `PATH`, or `pip` and `python` point to different
environments.

Prefer:

```bash
python -m pip install claude-p
python -m pip show -f claude-p
which claude-p
```

For conda/venv users:

```bash
conda activate your-env
python -m pip install claude-p
which python
which claude-p
claude-p "Respond exactly: OK"
```

## Development and release flow

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

## License

MIT
