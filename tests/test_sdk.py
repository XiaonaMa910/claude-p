from claude_p import ClaudePOptions
from claude_p.cli import classify_failure


def test_options_command_includes_stream_json():
    cmd = ClaudePOptions(model="sonnet", tools="").command("hello")
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--tools" in cmd
    assert "" in cmd


def test_console_scripts_declared():
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path("pyproject.toml").read_text())
    scripts = data["project"]["scripts"]
    assert scripts["claude-p"] == "claude_p.cli:main"
    assert scripts["claude-p.py"] == "claude_p.cli:main"


def test_rate_limit_is_error_even_when_tui_contains_text():
    transcript = "You've hit your limit · resets May 17 at 10am (Asia/Shanghai)"
    assert classify_failure(transcript, "You've hit your limit", timed_out=False) == "rate_limit"


def test_successful_assistant_text_is_not_failure():
    assert classify_failure("normal transcript", "CLAUDE_P_OK", timed_out=False) is None
