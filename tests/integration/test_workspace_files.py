"""Real file operations: bounded reads/search, exact atomic edits and permissions."""
import json

import pytest


async def call(hub, name, arguments, execution="automatic"):
    return await hub.wait(hub.submit({"name": name, "steps": [{"id": "file", "target": "files." + name,
                                                             "input": arguments}]}, execution=execution))


async def test_write_read_edit_find_and_grep_real_files(hub, tmp_path):
    await hub.execution.configure({"terminal_enabled": True, "workspace": str(tmp_path)})
    content = "标题\r\nvalue = 1\r\nother = 2\r\n"
    run = await call(hub, "write", {"path": "nested/sample.py", "content": content})
    assert run["status"] == "succeeded", run
    read = await call(hub, "read", {"path": "nested/sample.py", "offset": 2, "limit": 1})
    output = read["steps"][0]["output"]
    assert output["content"] == "value = 1\r\n" and output["next_offset"] == 3
    changed = await call(hub, "edit", {"path": "nested/sample.py", "expected_sha256": output["sha256"],
                                     "edits": [{"old_text": "value = 1", "new_text": "value = 3"},
                                               {"old_text": "other = 2", "new_text": "other = 4"}]})
    assert changed["status"] == "succeeded", changed
    assert (tmp_path / "nested/sample.py").read_bytes() == content.replace("= 1", "= 3").replace("= 2", "= 4").encode()
    assert "+value = 3" in changed["steps"][0]["output"]["diff"]
    found = await call(hub, "find", {"path": "nested", "glob": "*.py"})
    assert found["steps"][0]["output"]["files"] == ["sample.py"]
    searched = await call(hub, "grep", {"path": "nested", "pattern": "=", "limit": 1})
    assert searched["status"] == "succeeded", searched
    assert searched["steps"][0]["output"]["truncated"] is True
    assert searched["steps"][0]["output"]["matches"][0]["line"] == 2
    stale = await call(hub, "edit", {"path": "nested/sample.py", "expected_sha256": output["sha256"],
                                   "edits": [{"old_text": "value = 3", "new_text": "value = 9"}]})
    assert stale["status"] == "failed" and "changed since" in stale["steps"][0]["error"]


@pytest.mark.parametrize("edits", [
    [{"old_text": "a", "new_text": "b"}],
    [{"old_text": "value", "new_text": "x"}, {"old_text": "value = 1", "new_text": "y"}],
    [{"old_text": "value = 1", "new_text": "new"}, {"old_text": "missing", "new_text": "x"}],
])
async def test_ambiguous_overlapping_or_missing_edits_leave_original_untouched(hub, tmp_path, edits):
    await hub.execution.configure({"terminal_enabled": True, "workspace": str(tmp_path)})
    file = tmp_path / "test.txt"
    file.write_text("a a\nvalue = 1\n")
    original = file.read_bytes()
    result = await call(hub, "edit", {"path": "test.txt", "edits": edits})
    assert result["status"] == "failed", result
    assert file.read_bytes() == original


async def test_workspace_permissions_binary_and_write_approval(hub, tmp_path):
    assert not any(t["name"].startswith("files.") for t in hub.available_tools())
    assert (await call(hub, "find", {}))["status"] == "failed"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await hub.execution.configure({"terminal_enabled": True, "workspace": str(workspace)})
    (tmp_path / "outside.txt").write_text("private")
    (workspace / "link").symlink_to(tmp_path / "outside.txt")
    for path in ("../outside.txt", "link", str(tmp_path / "outside.txt")):
        run = await call(hub, "read", {"path": path})
        assert run["status"] == "failed"
        assert "private" not in json.dumps(run["steps"][0]["output"])
    (workspace / "binary").write_bytes(b"\0data")
    assert (await call(hub, "read", {"path": "binary"}))["status"] == "failed"
    paused = await call(hub, "write", {"path": "approved.txt", "content": "ok"}, execution="confirm")
    assert paused["status"] == "waiting_approval" and not (workspace / "approved.txt").exists()
    hub.tools.approve(hub.store, paused["approvals"][0]["id"], True)
    assert (await hub.wait(paused["id"]))["status"] == "succeeded"
    assert (workspace / "approved.txt").read_text() == "ok"
