"""The seam works: claude-graph reaches claude-ls's primitives through the adapter."""

from claude_graph import sessions


def test_adapter_reexports_primitives():
    assert callable(sessions.derive_work_signals)
    assert callable(sessions.canonical_repo_root)
    assert callable(sessions.list_projects)
    assert callable(sessions.list_sessions)
    assert sessions.WorkSignals is not None


def test_pr1_primitive_reachable_through_seam():
    entries = [
        {
            "type": "assistant",
            "cwd": "/w",
            "message": {
                "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}}]
            },
        }
    ]
    ws = sessions.derive_work_signals(entries)
    assert ws.edited_paths == ["/w/a.py"]  # PR1's edited_paths, reached via the adapter


def test_projects_dir_points_at_store():
    assert sessions.projects_dir().name == "projects"
    assert sessions.projects_dir().parent.name == ".claude"
