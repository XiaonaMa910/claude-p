from claude_p import ClaudePOptions


def test_options_command_includes_stream_json():
    cmd = ClaudePOptions(model="sonnet", tools="").command("hello")
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--tools" in cmd
    assert "" in cmd

