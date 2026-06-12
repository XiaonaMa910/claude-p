#!/usr/bin/env python3
"""Claude Code interactive-TUI backend with `claude -p` compatible output.

This script does not invoke `claude -p`. It starts interactive `claude` under a
pseudo-TTY, captures the rendered terminal, extracts the assistant answer, and
emits text/json/stream-json output shaped like `claude -p`.

Compatibility target:
- Same line-oriented JSON transport.
- Same core event families: system init, stream_event message_start,
  content_block_start/delta/stop, assistant, message_delta, message_stop, result.
- Usage, cost, model and rate-limit fields carry real values read from Claude
  Code's session JSONL and statusline payload. Tool events are not replayed.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import uuid


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
SPINNER_RE = re.compile(r"\n?[✳✶✻✽✢·].*$", re.DOTALL)
NON_TERMINAL_STOP_REASONS = {"tool_use", "pause_turn"}
SUBSCRIPTION_BACKEND_ENV_OVERRIDES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
# Markers a parent Claude Code session injects into child processes. If the
# spawned TUI inherits these it treats itself as a nested child session and —
# critically — CLAUDE_CODE_CHILD_SESSION=1 disables session JSONL persistence,
# which claude-p depends on for final text, usage and cost. Always strip them.
NESTED_CLAUDE_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
)
VARIADIC_PROMPT_ATTRS = (
    "tools",
    "allowed_tools",
    "disallowed_tools",
    "add_dir",
    "files",
    "mcp_config",
    "betas",
)


def warn(message: str) -> None:
    print(f"claude_tui_agent.py: warning: {message}", file=sys.stderr)


def append_flag(cmd: list[str], enabled: bool, flag: str) -> None:
    if enabled:
        cmd.append(flag)


def append_value(cmd: list[str], flag: str, value: str | None) -> None:
    if value is not None:
        cmd.extend([flag, value])


def append_optional_value(cmd: list[str], flag: str, value: str | None) -> None:
    if value is None:
        return
    cmd.append(flag)
    if value:
        cmd.append(value)


def append_repeated_values(cmd: list[str], flag: str, values: list[str] | None) -> None:
    values = flatten_cli_values(values)
    if not values:
        return
    for value in values:
        cmd.extend([flag, value])


def append_variadic_values(cmd: list[str], flag: str, values: list[str] | None) -> None:
    values = flatten_cli_values(values)
    if not values:
        return
    cmd.append(flag)
    cmd.extend(values)


def now_ms(start: float) -> int:
    return int((time.time() - start) * 1000)


def emit(obj: dict, enabled: bool = True) -> None:
    if enabled:
        print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), flush=True)


def clean_terminal(text: str) -> str:
    text = OSC_RE.sub("", text)
    text = ANSI_RE.sub("", text)
    return text.replace("\r", "").replace("\u00a0", " ")


def compact_for_detection(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_terminal(text).lower())


def flatten_cli_values(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    flattened: list[str] = []
    for value in values:
        if isinstance(value, str):
            flattened.append(value)
        elif isinstance(value, (list, tuple)):
            flattened.extend(str(item) for item in value)
        else:
            flattened.append(str(value))
    return flattened


def recover_prompt_from_variadic_args(args: argparse.Namespace) -> None:
    """Recover prompt-last invocations after argparse variadic option capture.

    Claude's CLI accepts options such as `--tools Bash Edit` as variadic lists,
    while this wrapper also accepts a free-form positional prompt. Argparse has
    no way to know where a variadic option ends when the prompt is last, so if
    no prompt was parsed and stdin is interactive, treat the final captured
    variadic token as the prompt.
    """
    if args.prompt is not None or not sys.stdin.isatty():
        return
    for attr in VARIADIC_PROMPT_ATTRS:
        values = flatten_cli_values(getattr(args, attr, None))
        if len(values) > 1:
            args.prompt = values.pop()
            setattr(args, attr, values)
            return


def normalize_answer(text: str) -> str:
    text = clean_terminal(text)
    text = SPINNER_RE.sub("", text)
    # Drop common TUI chrome if it leaked into the block.
    text = re.split(r"\n?────────────────", text, maxsplit=1)[0]
    return text.strip()


def extract_assistant_snapshot(transcript: str) -> str:
    clean = clean_terminal(transcript)
    marker = clean.rfind("⏺")
    if marker < 0:
        return ""
    after = clean[marker + len("⏺") :]
    return normalize_answer(after)


def classify_failure(transcript: str, assistant_text: str, timed_out: bool) -> str | None:
    interactive_block = classify_interactive_block(f"{transcript}\n{assistant_text}")
    if interactive_block:
        return interactive_block
    if assistant_text:
        return None
    if timed_out:
        return "assistant_output_timeout"
    return "assistant_output_not_found"


def classify_interactive_block(text: str) -> str | None:
    low = clean_terminal(text).lower()
    compact = compact_for_detection(text)
    if "failed to authenticate" in low or "api error: 403" in low or "pleaserunlogin" in compact:
        return "auth_blocked"
    if "you've hit your limit" in low or "you have hit your limit" in low or "hit your limit" in low:
        return "rate_limit"
    if (
        ("do you trust" in low and "folder" in low)
        or "workspacetrust" in compact
        or ("quicksafetycheck" in compact and ("itrustthisfolder" in compact or "accessingworkspace" in compact))
    ):
        return "workspace_trust_blocked"
    if "bypasspermissionsmode" in compact and "iaccept" in compact:
        # One-time TUI consent dialog for --dangerously-skip-permissions.
        # Accept it manually once per machine, or run claude-p with
        # --accept-bypass-permissions to record the consent automatically.
        return "bypass_permissions_dialog_blocked"
    if "permission" in low and ("allow" in low or "deny" in low):
        return "tool_approval_blocked"
    return None


def build_usage(output_text: str, persisted_usage: dict | None = None) -> dict:
    """Build the result-event usage dict.

    Claude Code persists the real Anthropic API usage of every assistant turn in
    the session JSONL. When available, forward the final turn's usage verbatim —
    this matches native `claude -p`, whose result.usage is the last main-loop API
    call's usage. Fall back to the legacy placeholder shape otherwise.
    """
    approx_output_tokens = max(1, len(output_text.split()))
    if isinstance(persisted_usage, dict) and persisted_usage.get("input_tokens") is not None:
        return dict(persisted_usage)
    return {
        "input_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "output_tokens": approx_output_tokens,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": None,
        "cache_creation": {"ephemeral_1h_input_tokens": None, "ephemeral_5m_input_tokens": None},
        "iterations": [
            {
                "input_tokens": None,
                "output_tokens": approx_output_tokens,
                "cache_read_input_tokens": None,
                "cache_creation_input_tokens": None,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": None,
                    "ephemeral_1h_input_tokens": None,
                },
                "type": "message",
            }
        ],
        "speed": None,
    }


# ---------------------------------------------------------------------------
# Real usage / cost / rate-limit reconstruction.
#
# Native `claude -p` reports total_cost_usd, usage and modelUsage on the result
# event, and emits rate_limit_event with rate_limit_info. The interactive TUI
# has no machine protocol, but two byproducts carry the same data:
#   1. The session JSONL: every assistant turn line holds the API `usage` and
#      `model`. NOTE: one API response is written as N lines (one per content
#      block) with the SAME message id and usage — dedupe by id before summing.
#   2. The statusline hook: Claude Code feeds it a JSON payload per refresh
#      containing cost.total_cost_usd (Claude's own price — never priced
#      locally) and rate_limits.{five_hour,seven_day}.{used_percentage,
#      resets_at}. We inject a statusline command that dumps the latest
#      payload to a temp file (unless the caller passed --settings).
# ---------------------------------------------------------------------------


def read_session_usage(session_id: str, since_iso: str, *, wait_seconds: float = 2.0) -> dict:
    """Aggregate per-model token usage from the session JSONL, deduped by message id.

    Tokens only — no price math here. Cost comes from Claude Code itself via the
    statusline payload (cost.total_cost_usd), the same number native claude -p
    reports; this wrapper never prices tokens with its own tables.

    `since_iso` (ISO-8601 UTC) excludes turns persisted before this run started,
    so --resume runs only report their own usage — matching native claude -p.
    Returns {"modelUsage": {...}, "last_model": str|None}.
    """
    empty = {"modelUsage": {}, "last_model": None}
    pattern = str(Path.home() / ".claude" / "projects" / "**" / f"{session_id}.jsonl")
    deadline = time.monotonic() + wait_seconds
    while True:
        paths = [Path(p) for p in glob.glob(pattern, recursive=True)]
        if paths or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if not paths:
        return empty
    path = max(paths, key=lambda p: p.stat().st_mtime)

    # message id -> (model, usage); later lines for the same id overwrite so the
    # final (complete) snapshot of each API response wins.
    by_message: dict[str, tuple[str, dict]] = {}
    last_model: str | None = None
    try:
        with path.open() as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant":
                    continue
                ts = event.get("timestamp")
                if isinstance(ts, str) and ts < since_iso:
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                msg_id = message.get("id") or event.get("requestId") or event.get("uuid")
                model = message.get("model") or "unknown"
                by_message[msg_id] = (model, usage)
                last_model = model
    except OSError:
        return empty

    model_usage: dict[str, dict] = {}
    for model, usage in by_message.values():
        bucket = model_usage.setdefault(
            model,
            {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": None,
            },
        )
        bucket["inputTokens"] += usage.get("input_tokens") or 0
        bucket["outputTokens"] += usage.get("output_tokens") or 0
        bucket["cacheReadInputTokens"] += usage.get("cache_read_input_tokens") or 0
        bucket["cacheCreationInputTokens"] += usage.get("cache_creation_input_tokens") or 0
        bucket["webSearchRequests"] += (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    return {"modelUsage": model_usage, "last_model": last_model}


def statusline_snapshot_path(session_id: str) -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / f"claude-p-status-{session_id}.json"


def read_statusline_snapshot(session_id: str) -> dict:
    """Read the latest statusline payload for this session.

    Primary source: the snapshot file written by our injected statusline
    command. Fallback: ~/.claude/usage.jsonl when the user's own statusline
    config (which claude-p then doesn't override) appends payloads there.
    """
    path = statusline_snapshot_path(session_id)
    try:
        if path.exists() and path.stat().st_size > 0:
            return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    usage_log = Path.home() / ".claude" / "usage.jsonl"
    latest: dict = {}
    try:
        with usage_log.open() as f:
            for line in f:
                if session_id not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = record.get("data") or {}
                if data.get("session_id") == session_id:
                    latest = data
    except OSError:
        pass
    return latest


def build_rate_limit_info(snapshot: dict, *, rejected: bool) -> dict:
    """Shape statusline rate_limits into native rate_limit_info.

    Native shape: {"status": "allowed"|"rejected", "resetsAt": epoch,
    "rateLimitType": "five_hour"|"seven_day", ...}. The statusline payload's
    rate_limits.*.resets_at carries the exact same epoch the native event uses.
    """
    limits = snapshot.get("rate_limits") or {}
    candidates = [
        (name, info)
        for name, info in limits.items()
        if isinstance(info, dict) and info.get("resets_at")
    ]
    if not candidates:
        return {"status": "rejected" if rejected else "unknown"}
    name, info = max(candidates, key=lambda kv: kv[1].get("used_percentage") or 0)
    return {
        "status": "rejected" if rejected else "allowed",
        "resetsAt": info.get("resets_at"),
        "rateLimitType": name,
        "used_percentage": info.get("used_percentage"),
    }


def ensure_bypass_permissions_accepted() -> None:
    """Pre-accept the TUI's one-time Bypass Permissions dialog.

    Native `claude -p --dangerously-skip-permissions` runs headless, but the
    interactive TUI blocks on a one-time acceptance dialog (default: "No, exit")
    until ~/.claude.json records bypassPermissionsModeAccepted. Only called when
    the operator explicitly opts in via --accept-bypass-permissions.
    """
    path = Path.home() / ".claude.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return
    if data.get("bypassPermissionsModeAccepted") is True:
        return
    data["bypassPermissionsModeAccepted"] = True
    tmp = path.with_name(".claude.json.claude-p-tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(path)
    except OSError:
        pass


def build_tui_env(args: argparse.Namespace) -> dict[str, str]:
    env = {**os.environ, "NO_COLOR": "1", "TERM": args.term}
    if not args.preserve_provider_env:
        for name in SUBSCRIPTION_BACKEND_ENV_OVERRIDES:
            env.pop(name, None)
    for name in NESTED_CLAUDE_SESSION_ENV:
        env.pop(name, None)
    return env


def extract_text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def canonical_json_if_equivalent(left: str, right: str) -> str | None:
    try:
        left_obj = json.loads(left)
        right_obj = json.loads(right)
    except json.JSONDecodeError:
        return None
    if left_obj != right_obj:
        return None
    return json.dumps(right_obj, ensure_ascii=False, separators=(",", ":"))


def is_terminal_assistant_message(message: dict) -> bool:
    stop_reason = message.get("stop_reason")
    return stop_reason is not None and stop_reason not in NON_TERMINAL_STOP_REASONS


def read_persisted_assistant(session_id: str, *, require_terminal: bool = False) -> dict | None:
    """Read Claude Code's persisted JSONL for exact final assistant text.

    The interactive terminal is a lossy rendering surface: wide glyphs, cursor
    redraws, and spinner updates can drop or smear characters in the captured
    TTY transcript. Claude Code still writes the canonical session JSONL for
    interactive sessions. When available, use it as the source of truth for the
    final assistant message while keeping the TUI transcript as provenance.
    """
    pattern = str(Path.home() / ".claude" / "projects" / "**" / f"{session_id}.jsonl")
    paths = [Path(p) for p in glob.glob(pattern, recursive=True)]
    if not paths:
        return None
    path = max(paths, key=lambda p: p.stat().st_mtime)
    latest: dict | None = None
    try:
        with path.open() as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant":
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                text = extract_text_from_content(message.get("content"))
                if not text:
                    continue
                terminal = is_terminal_assistant_message(message)
                if require_terminal and not terminal:
                    continue
                latest = {
                    "path": str(path),
                    "text": text,
                    "message": message,
                    "model": message.get("model"),
                    "message_id": message.get("id"),
                    "usage": message.get("usage"),
                    "stop_reason": message.get("stop_reason"),
                    "terminal": terminal,
                }
    except OSError:
        return None
    return latest


def run_tui(args: argparse.Namespace, stream_json: bool) -> tuple[str, str, int | None, bool, float]:
    # Opt-in only: silently flipping a consent flag in ~/.claude.json is a
    # security-relevant side effect, so it requires this explicit wrapper flag
    # in addition to --dangerously-skip-permissions itself.
    if args.accept_bypass_permissions and (
        args.dangerously_skip_permissions or args.allow_dangerously_skip_permissions
    ):
        ensure_bypass_permissions_accepted()
    cmd = ["claude", "--session-id", args.session_id]

    # Pass through options that the interactive `claude` entrypoint itself
    # understands. Print-only options are handled by this wrapper and are not
    # forwarded.
    append_variadic_values(cmd, "--add-dir", args.add_dir)
    append_value(cmd, "--agent", args.agent)
    append_value(cmd, "--agents", args.agents)
    append_flag(cmd, args.allow_dangerously_skip_permissions, "--allow-dangerously-skip-permissions")
    append_variadic_values(cmd, "--allowedTools", args.allowed_tools)
    append_value(cmd, "--append-system-prompt", args.append_system_prompt)
    append_variadic_values(cmd, "--betas", args.betas)
    append_flag(cmd, args.brief, "--brief")
    append_flag(cmd, args.chrome, "--chrome")
    append_flag(cmd, args.no_chrome, "--no-chrome")
    append_flag(cmd, args.continue_session, "--continue")
    append_flag(cmd, args.dangerously_skip_permissions, "--dangerously-skip-permissions")
    append_optional_value(cmd, "--debug", args.debug)
    append_value(cmd, "--debug-file", args.debug_file)
    append_flag(cmd, args.disable_slash_commands, "--disable-slash-commands")
    append_variadic_values(cmd, "--disallowedTools", args.disallowed_tools)
    append_value(cmd, "--effort", args.effort)
    append_flag(cmd, args.exclude_dynamic_system_prompt_sections, "--exclude-dynamic-system-prompt-sections")
    append_variadic_values(cmd, "--file", args.files)
    append_flag(cmd, args.fork_session, "--fork-session")
    append_optional_value(cmd, "--from-pr", args.from_pr)
    append_flag(cmd, args.ide, "--ide")
    append_value(cmd, "--json-schema", args.json_schema)
    append_variadic_values(cmd, "--mcp-config", args.mcp_config)
    append_flag(cmd, args.mcp_debug, "--mcp-debug")
    append_variadic_values(cmd, "--tools", args.tools)
    append_value(cmd, "--model", args.model)
    append_value(cmd, "--name", args.name)
    append_value(cmd, "--permission-mode", args.permission_mode)
    append_repeated_values(cmd, "--plugin-dir", args.plugin_dir)
    append_repeated_values(cmd, "--plugin-url", args.plugin_url)
    append_optional_value(cmd, "--remote-control", args.remote_control)
    append_value(cmd, "--remote-control-session-name-prefix", args.remote_control_session_name_prefix)
    append_optional_value(cmd, "--resume", args.resume)
    append_value(cmd, "--setting-sources", args.setting_sources)
    # Inject a statusline that dumps Claude Code's per-refresh payload (cost +
    # rate_limits) to a temp file, so the result/rate_limit events can report
    # real data. Skipped when the caller supplies --settings — theirs wins.
    if args.settings is None:
        snapshot_path = statusline_snapshot_path(args.session_id)
        injected = {
            "statusLine": {
                "type": "command",
                "command": f"cat > {snapshot_path}",
                "padding": 0,
            }
        }
        cmd.extend(["--settings", json.dumps(injected)])
    else:
        append_value(cmd, "--settings", args.settings)
    append_flag(cmd, args.strict_mcp_config, "--strict-mcp-config")
    append_value(cmd, "--system-prompt", args.system_prompt)
    append_optional_value(cmd, "--tmux", args.tmux)
    append_optional_value(cmd, "--worktree", args.worktree)

    cmd.append(args.prompt)
    master, slave = pty.openpty()
    env = build_tui_env(args)
    start = time.time()
    proc = subprocess.Popen(
        cmd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=args.cwd,
        env=env,
    )
    os.close(slave)

    raw = bytearray()
    last_output = time.time()
    last_snapshot = ""
    last_jsonl_poll = 0.0
    timed_out = True

    try:
        while time.time() - start < args.timeout_sec:
            now = time.time()
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                raw.extend(data)
                last_output = time.time()

                if args.emit_terminal_delta:
                    emit(
                        {
                            "type": "tui_terminal_delta",
                            "text": clean_terminal(data.decode("utf-8", "replace")),
                            "uuid": str(uuid.uuid4()),
                            "session_id": args.session_id,
                        },
                        enabled=stream_json,
                    )

                snapshot = extract_assistant_snapshot(raw.decode("utf-8", "replace"))
                if snapshot and snapshot != last_snapshot:
                    if args.live_tui_deltas:
                        delta = snapshot[len(last_snapshot) :] if snapshot.startswith(last_snapshot) else snapshot
                        if delta.strip():
                            emit(
                                {
                                    "type": "stream_event",
                                    "event": {
                                        "type": "content_block_delta",
                                        "index": 0,
                                        "delta": {"type": "text_delta", "text": delta},
                                    },
                                    "session_id": args.session_id,
                                    "parent_tool_use_id": None,
                                    "uuid": str(uuid.uuid4()),
                                },
                                enabled=stream_json,
                            )
                    last_snapshot = snapshot

            transcript = raw.decode("utf-8", "replace")
            if classify_interactive_block(transcript):
                timed_out = False
                break

            # The terminal surface is not a stable completion signal across
            # Claude Code versions and terminal modes. Poll the canonical
            # session JSONL while the TUI is running, and finish only after the
            # current session has a terminal assistant message. Tool-use turns
            # can also contain text and must not be mistaken for final output.
            if now - last_jsonl_poll >= 0.5:
                last_jsonl_poll = now
                persisted = read_persisted_assistant(args.session_id)
                if persisted and persisted.get("text") and persisted.get("terminal"):
                    timed_out = False
                    break

            if last_snapshot and time.time() - last_output >= args.quiet_after_sec:
                persisted = read_persisted_assistant(args.session_id)
                if not persisted or persisted.get("terminal"):
                    timed_out = False
                    break
            if proc.poll() is not None:
                timed_out = False
                break
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        os.close(master)

    transcript = clean_terminal(raw.decode("utf-8", "replace"))
    answer = extract_assistant_snapshot(transcript)
    return transcript, answer, proc.returncode, timed_out, start


def doctor(args: argparse.Namespace) -> int:
    """Print diagnostics that explain most installation and local CLI failures."""
    print("claude-p doctor")
    print(f"invoked_as: {sys.argv[0]}")
    print(f"python: {sys.executable}")
    print(f"python_version: {sys.version.split()[0]}")
    print(f"cwd: {args.cwd}")
    print(f"home: {Path.home()}")

    claude_path = shutil.which("claude")
    print(f"claude_path: {claude_path or 'not found'}")
    if claude_path:
        try:
            proc = subprocess.run(
                ["claude", "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            version = (proc.stdout or proc.stderr).strip()
            print(f"claude_version: {version or 'unknown'}")
        except Exception as exc:  # pragma: no cover - defensive diagnostic path.
            print(f"claude_version_error: {exc}")

    session_root = Path.home() / ".claude" / "projects"
    print(f"session_root: {session_root}")
    print(f"session_root_exists: {session_root.exists()}")
    print(f"session_root_writable: {os.access(session_root, os.W_OK) if session_root.exists() else False}")

    claude_p_path = shutil.which("claude-p")
    print(f"claude_p_path: {claude_p_path or 'not found'}")
    present_overrides = [name for name in SUBSCRIPTION_BACKEND_ENV_OVERRIDES if os.environ.get(name)]
    print(f"provider_env_overrides_present: {','.join(present_overrides) if present_overrides else 'none'}")
    print(f"provider_env_policy: {'preserve' if args.preserve_provider_env else 'strip_for_subscription_backend'}")
    print("smoke_test:")
    print('  claude-p "Respond exactly: CLAUDE_P_OK" --timeout-sec 45 --quiet-after-sec 2 --raw-log /tmp/claude-p-smoke.raw.log')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--cwd", default=os.getcwd())

    # Common Claude Code options. The goal is CLI compatibility with the print
    # path while still using the interactive TUI backend internally.
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true", help="Accepted for claude -p compatibility.")
    parser.add_argument("--add-dir", nargs="+", action="append", default=[])
    parser.add_argument("--agent")
    parser.add_argument("--agents")
    parser.add_argument("--allow-dangerously-skip-permissions", action="store_true")
    parser.add_argument("--allowedTools", "--allowed-tools", dest="allowed_tools", nargs="+", action="append", default=[])
    parser.add_argument("--append-system-prompt")
    parser.add_argument("--bare", action="store_true")
    parser.add_argument("--betas", nargs="+", action="append", default=[])
    parser.add_argument("--brief", action="store_true")
    parser.add_argument("--chrome", action="store_true")
    parser.add_argument("--no-chrome", action="store_true")
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("-d", "--debug", nargs="?", const="")
    parser.add_argument("--debug-file")
    parser.add_argument("--disable-slash-commands", action="store_true")
    parser.add_argument("--disallowedTools", "--disallowed-tools", dest="disallowed_tools", nargs="+", action="append", default=[])
    parser.add_argument("--effort")
    parser.add_argument("--exclude-dynamic-system-prompt-sections", action="store_true")
    parser.add_argument("--fallback-model")
    parser.add_argument("--file", dest="files", nargs="+", action="append", default=[])
    parser.add_argument("--fork-session", action="store_true")
    parser.add_argument("--from-pr", nargs="?", const="")
    parser.add_argument("--ide", action="store_true")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--tools", nargs="+", default=["default"])
    parser.add_argument("--permission-mode", default="default")
    parser.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        help="Output format, matching claude -p. Default: text.",
    )
    parser.add_argument("--verbose", action="store_true", help="Accepted for claude -p CLI compatibility.")
    parser.add_argument("--include-hook-events", action="store_true")
    parser.add_argument(
        "--include-partial-messages",
        action="store_true",
        help="Accepted for claude -p CLI compatibility. With the TUI backend, stream-json emits one final text delta by default.",
    )
    parser.add_argument("--input-format", choices=["text", "stream-json"], default="text")
    parser.add_argument("--json-schema")
    parser.add_argument("--max-budget-usd")
    parser.add_argument("--mcp-config", nargs="+", action="append", default=[])
    parser.add_argument("--mcp-debug", action="store_true")
    parser.add_argument("-n", "--name")
    parser.add_argument("--no-session-persistence", action="store_true")
    parser.add_argument("--plugin-dir", action="append", default=[])
    parser.add_argument("--plugin-url", action="append", default=[])
    parser.add_argument("--remote-control", nargs="?", const="")
    parser.add_argument("--remote-control-session-name-prefix")
    parser.add_argument("--replay-user-messages", action="store_true")
    parser.add_argument("-r", "--resume", nargs="?", const="")
    parser.add_argument("--setting-sources")
    parser.add_argument("--settings")
    parser.add_argument("--strict-mcp-config", action="store_true")
    parser.add_argument("--system-prompt")
    parser.add_argument("--tmux", nargs="?", const="")
    parser.add_argument("-v", "--version", action="store_true")
    parser.add_argument("-w", "--worktree", nargs="?", const="")

    # Wrapper-only controls.
    parser.add_argument("--timeout-sec", type=float, default=90)
    parser.add_argument("--quiet-after-sec", type=float, default=3)
    parser.add_argument("--session-id", default=str(uuid.uuid4()))
    parser.add_argument("--term", default="xterm-256color")
    parser.add_argument("--raw-log")
    parser.add_argument(
        "--preserve-provider-env",
        action="store_true",
        help="Preserve Anthropic API/provider environment variables instead of stripping them for the subscription-backed TUI.",
    )
    parser.add_argument(
        "--accept-bypass-permissions",
        action="store_true",
        help=(
            "With --dangerously-skip-permissions: pre-record the TUI's one-time "
            "Bypass Permissions consent in ~/.claude.json so headless runs don't "
            "block on the dialog. Off by default because it flips a security consent."
        ),
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print local installation diagnostics without calling the model.",
    )
    parser.add_argument("--emit-terminal-delta", action="store_true")
    parser.add_argument(
        "--live-tui-deltas",
        action="store_true",
        help="Emit live text deltas from the lossy TUI surface. Default buffers until persisted JSONL final text is available.",
    )
    args = parser.parse_args()
    recover_prompt_from_variadic_args(args)

    if args.doctor:
        return doctor(args)

    if args.version:
        subprocess.run(["claude", "--version"], check=False)
        return 0

    if args.prompt is None:
        if sys.stdin.isatty():
            parser.error("prompt is required unless stdin provides input")
        args.prompt = sys.stdin.read()

    unsupported: list[str] = []
    if args.input_format != "text":
        unsupported.append("--input-format stream-json")
    if args.replay_user_messages:
        unsupported.append("--replay-user-messages")
    if args.no_session_persistence:
        unsupported.append("--no-session-persistence")
    if args.bare:
        unsupported.append("--bare")
    if args.max_budget_usd:
        unsupported.append("--max-budget-usd")
    if args.fallback_model:
        unsupported.append("--fallback-model")
    if unsupported:
        for flag in unsupported:
            warn(f"{flag} is not supported by the interactive subscription backend; continuing without exact claude -p semantics")

    stream_json = args.output_format == "stream-json"
    message_id = f"msg_tui_{uuid.uuid4().hex[:24]}"
    start = time.time()

    emit(
        {
            "type": "system",
            "subtype": "init",
            "cwd": args.cwd,
            "session_id": args.session_id,
            "tools": [],
            "mcp_servers": [],
            "model": args.model,
            "permissionMode": args.permission_mode,
            "apiKeySource": "interactive_tui_subscription",
            "claude_code_version": None,
            "output_style": "default",
            "uuid": str(uuid.uuid4()),
            "fast_mode_state": "off",
        },
        enabled=stream_json,
    )
    emit(
        {
            "type": "system",
            "subtype": "status",
            "status": "requesting",
            "uuid": str(uuid.uuid4()),
            "session_id": args.session_id,
        },
        enabled=stream_json,
    )
    emit(
        {
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {
                    "model": args.model,
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "stop_details": None,
                    "usage": {
                        "input_tokens": None,
                        "cache_creation_input_tokens": None,
                        "cache_read_input_tokens": None,
                        "output_tokens": None,
                        "service_tier": None,
                    },
                },
            },
            "session_id": args.session_id,
            "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()),
            "ttft_ms": None,
        },
        enabled=stream_json,
    )
    emit(
        {
            "type": "stream_event",
            "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            "session_id": args.session_id,
            "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()),
        },
        enabled=stream_json,
    )

    transcript, tui_answer, exit_code, timed_out, run_start = run_tui(args, stream_json)
    if args.raw_log:
        path = Path(args.raw_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(transcript)

    persisted = read_persisted_assistant(args.session_id, require_terminal=True)
    answer = persisted["text"] if persisted else tui_answer
    final_answer_source = "session_jsonl" if persisted else "tui_transcript"
    if persisted and tui_answer and tui_answer != persisted["text"]:
        canonical = canonical_json_if_equivalent(tui_answer, persisted["text"])
        if canonical is not None:
            answer = canonical
            final_answer_source = "json_canonicalized_from_matching_tui_and_session_jsonl"
    final_model = persisted.get("model") if persisted else args.model
    message_id = persisted.get("message_id") if persisted and persisted.get("message_id") else message_id

    if answer and not args.live_tui_deltas:
        emit(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": answer},
                },
                "session_id": args.session_id,
                "parent_tool_use_id": None,
                "uuid": str(uuid.uuid4()),
            },
            enabled=stream_json,
        )

    failure = classify_failure(transcript, answer, timed_out)
    is_error = failure is not None

    # Reconstruct what native claude -p reports natively: usage from the final
    # assistant turn, modelUsage token sums over this run's API calls (deduped
    # by message id), price and rate limits from Claude Code's own statusline
    # payload — no locally computed prices.
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start - 30))
    session_usage = read_session_usage(args.session_id, since_iso)
    usage = build_usage(answer, persisted.get("usage") if persisted else None)
    model_usage = session_usage["modelUsage"]
    snapshot = read_statusline_snapshot(args.session_id)
    # Price comes from Claude Code itself (statusline payload cost.total_cost_usd
    # — the same number native claude -p puts in result.total_cost_usd). No local
    # pricing tables: if Claude didn't report a cost, report None.
    snapshot_cost = (snapshot.get("cost") or {}).get("total_cost_usd")
    total_cost_usd = snapshot_cost if isinstance(snapshot_cost, (int, float)) and snapshot_cost > 0 else None
    context_window_size = (snapshot.get("context_window") or {}).get("context_window_size")
    for bucket in model_usage.values():
        if context_window_size:
            bucket["contextWindow"] = context_window_size
    if total_cost_usd is not None and len(model_usage) == 1:
        # Single-model run: Claude's session cost belongs to that model.
        next(iter(model_usage.values()))["costUSD"] = total_cost_usd
    # Resolve the real model id. The early init event could only echo the CLI
    # alias (e.g. "haiku"); the session JSONL and the statusline payload carry
    # what Claude Code actually ran (e.g. "claude-haiku-4-5-20251001").
    if final_model == args.model:
        resolved_model = session_usage["last_model"] or (snapshot.get("model") or {}).get("id")
        if resolved_model:
            final_model = resolved_model
    rate_limit_info = build_rate_limit_info(snapshot, rejected=failure == "rate_limit")
    try:
        statusline_snapshot_path(args.session_id).unlink(missing_ok=True)
    except OSError:
        pass
    duration_ms = now_ms(start)

    if args.output_format == "text":
        if is_error:
            if answer:
                print(answer, file=sys.stderr)
            print(
                f"claude-p error: {failure}. "
                "Run with --raw-log /tmp/claude-p.raw.log and inspect the log if this is unexpected.",
                file=sys.stderr,
            )
        elif answer:
            print(answer)
        return 0 if not is_error else 2

    if args.output_format == "json":
        print(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success" if not is_error else "error",
                    "is_error": is_error,
                    "duration_ms": duration_ms,
                    "duration_api_ms": None,
                    "num_turns": 1,
                    "result": answer,
                    "session_id": args.session_id,
                    "total_cost_usd": total_cost_usd,
                    "modelUsage": model_usage,
                    "usage": usage,
                    "terminal_reason": "completed" if not is_error else failure,
                    "interactive_tui_backend": {
                        "raw_log": args.raw_log,
                        "session_jsonl": persisted.get("path") if persisted else None,
                        "tui_answer": tui_answer,
                        "final_answer_source": final_answer_source,
                        "timed_out": timed_out,
                        "exit_code": exit_code,
                        "extraction_confidence": "high" if persisted else ("medium" if answer else "none"),
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0 if not is_error else 2

    if final_model != args.model:
        # The first init event could only echo the CLI alias (emitted before the
        # TUI launched). Re-emit init carrying the resolved model id so parsers
        # that read the model from init (native claude -p puts the full id
        # there) end up with the real value.
        emit(
            {
                "type": "system",
                "subtype": "init",
                "cwd": args.cwd,
                "session_id": args.session_id,
                "tools": [],
                "mcp_servers": [],
                "model": final_model,
                "permissionMode": args.permission_mode,
                "apiKeySource": "interactive_tui_subscription",
                "claude_code_version": None,
                "output_style": "default",
                "model_resolved": True,
                "uuid": str(uuid.uuid4()),
                "fast_mode_state": "off",
            }
        )

    emit(
        {
            "type": "assistant",
            "message": {
                "model": final_model,
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": answer}] if answer else [],
                "stop_reason": "end_turn" if not is_error else None,
                "stop_sequence": None,
                "stop_details": None,
                "usage": {
                    "input_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                    "output_tokens": usage.get("output_tokens"),
                    "service_tier": None,
                },
                "context_management": None,
            },
            "parent_tool_use_id": None,
            "session_id": args.session_id,
            "uuid": str(uuid.uuid4()),
        }
    )
    emit(
        {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 0},
            "session_id": args.session_id,
            "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()),
        }
    )
    emit(
        {
            "type": "stream_event",
            "event": {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn" if not is_error else "error", "stop_sequence": None, "stop_details": None},
                "usage": {
                    "input_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                    "output_tokens": usage.get("output_tokens"),
                    "iterations": usage.get("iterations"),
                },
                "context_management": {"applied_edits": []},
            },
            "session_id": args.session_id,
            "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()),
        }
    )
    emit(
        {
            "type": "stream_event",
            "event": {"type": "message_stop"},
            "session_id": args.session_id,
            "parent_tool_use_id": None,
            "uuid": str(uuid.uuid4()),
        }
    )
    emit(
        {
            "type": "rate_limit_event",
            "rate_limit_info": rate_limit_info,
            "session_id": args.session_id,
            "uuid": str(uuid.uuid4()),
        }
    )
    emit(
        {
            "type": "result",
            "subtype": "success" if not is_error else "error",
            "is_error": is_error,
            "api_error_status": None,
            "duration_ms": duration_ms,
            "duration_api_ms": None,
            "num_turns": 1,
            "result": answer,
            "stop_reason": "end_turn" if not is_error else None,
            "session_id": args.session_id,
            "total_cost_usd": total_cost_usd,
            "usage": usage,
            "modelUsage": model_usage,
            "permission_denials": [],
            "terminal_reason": "completed" if not is_error else failure,
            "fast_mode_state": "off",
            "uuid": str(uuid.uuid4()),
            "interactive_tui_backend": {
                "raw_log": args.raw_log,
                "session_jsonl": persisted.get("path") if persisted else None,
                "tui_answer": tui_answer,
                "final_answer_source": final_answer_source,
                "timed_out": timed_out,
                "exit_code": exit_code,
                "extraction_confidence": "high" if persisted else ("medium" if answer else "none"),
                "compatibility_note": "Shape-compatible with claude -p stream-json core events; usage/cost/model/rate-limit are read from Claude Code's session JSONL and statusline payload; tool events are not replayed.",
            },
        }
    )

    return 0 if not is_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
