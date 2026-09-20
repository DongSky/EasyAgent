"""Packaged data and extension sources must work on non-UTF-8 Windows locales."""
import json
from pathlib import Path

from easyagent.extension_cli import package_directory
from easyagent.runtime import Hub


async def test_catalog_and_extension_files_ignore_legacy_system_encoding(tmp_path, monkeypatch):
    read_text, write_text = Path.read_text, Path.write_text

    def legacy_read(path, encoding=None, errors=None):
        return read_text(path, encoding=encoding or 'cp1252', errors=errors)

    def legacy_write(path, data, encoding=None, errors=None, newline=None):
        return write_text(path, data, encoding=encoding or 'cp1252', errors=errors, newline=newline)

    monkeypatch.setattr(Path, 'read_text', legacy_read)
    monkeypatch.setattr(Path, 'write_text', legacy_write)
    source = tmp_path / 'unicode-extension'
    source.mkdir()
    manifest = {'id': 'locale_check', 'revision': 1, 'title': '中文节点',
                'tools': [{'handler': 'echo', 'spec': {'name': 'locale_check.echo', 'description': '返回中文'}}]}
    (source / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    (source / 'sources.json').write_text('["extension.js"]', encoding='utf-8')
    (source / 'extension.js').write_text('function handle(r) { return {result: {text: "运行成功"}}; }',
                                       encoding='utf-8')
    hub = Hub(tmp_path / 'workspace.db', poll_seconds=.01)
    assert hub.model_catalog.operations
    await hub.start()
    try:
        package = package_directory(source)
        assert package.manifest.title == '中文节点'
        await hub.extensions.install({'package': package})
        # Repeated materialization exercises UTF-8 comparison against an existing source file.
        assert hub.extensions.materialize(package) == hub.extensions.materialize(package)
        run = await hub.wait(hub.submit({'name': '中文测试', 'steps': [
            {'id': 'echo', 'target': 'locale_check.echo'},
        ]}))
        assert run['status'] == 'succeeded'
        assert run['steps'][0]['output'] == {'text': '运行成功'}
    finally:
        await hub.stop()
