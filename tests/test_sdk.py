from claude_p import ClaudePOptions


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
