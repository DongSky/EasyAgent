"""Create portable desktop ZIPs, then smoke-test the extracted app without the checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request


def platform_target():
    system = {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform)
    machine = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(machine, machine)
    if system is None:
        raise ValueError('Portable desktop ZIPs currently target macOS and Windows')
    return f'{system}-{arch}'


def smoke(executable, directory, logs):
    # Credentials, Python paths and the caller's workspace must not influence the app.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'HOME', 'USERPROFILE',
        'LOCALAPPDATA', 'APPDATA', 'LANG', 'LC_ALL',
    }}
    env.update(EAH_DESKTOP_NO_BROWSER='1', EAH_DATA_DIR=str(directory / 'data'))
    with (logs / 'smoke.log').open('wb') as log:
        subprocess.run([str(executable), '--smoke'], cwd=directory, env=env,
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
        process = subprocess.Popen([str(executable)], cwd=directory, env=env,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'Application exited early ({process.returncode}); see smoke.log')
                state = directory / 'data/runtime.json'
                try:
                    origin = json.loads(state.read_text())['url']
                    with opener.open(origin + '/health', timeout=2) as response:
                        assert response.status == 200
                    break
                except (OSError, ValueError, urllib.error.URLError):
                    if time.monotonic() > deadline:
                        raise TimeoutError('Application failed to serve /health within 60 seconds')
                    time.sleep(.2)
            for route, content_type in [('/', 'text/html'), ('/assets/studio.js', 'javascript'),
                                         ('/life', 'text/html'), ('/openapi.json', 'application/json')]:
                with opener.open(origin + route, timeout=10) as response:
                    assert response.status == 200 and content_type in response.headers['Content-Type'], route
                    assert response.read(), route
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', help='Expected OS/architecture, checked against this build host')
    args = parser.parse_args()
    target = platform_target()
    if args.target and args.target != target:
        parser.error(f'Runner architecture mismatch: requested {args.target}, actual {target}')
    root = Path(__file__).resolve().parents[2]
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    build = root / '.eah/build'
    releases, logs = build / 'releases', build / 'logs'
    releases.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    bundle = 'EasyAgent.app' if sys.platform == 'darwin' else 'EasyAgent'
    source = build / 'desktop' / bundle
    if not source.is_dir():
        raise FileNotFoundError(f'Build the application first: {source}')
    archive = releases / f'EasyAgent-{version}-{target}.zip'
    archive.unlink(missing_ok=True)
    archive.with_suffix('.zip.sha256').unlink(missing_ok=True)
    if sys.platform == 'darwin':
        subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(source), str(archive)],
                       check=True)
    else:
        shutil.make_archive(str(archive.with_suffix('')), 'zip', root_dir=source.parent, base_dir=bundle)
    try:
        with tempfile.TemporaryDirectory(prefix='easyagent-desktop-') as tmp:
            directory = Path(tmp)
            if sys.platform == 'darwin':
                subprocess.run(['ditto', '-x', '-k', str(archive), str(directory)], check=True)
                executable = directory / bundle / 'Contents/MacOS/EasyAgent'
            else:
                shutil.unpack_archive(archive, directory)
                executable = directory / bundle / 'EasyAgent.exe'
            smoke(executable, directory, logs)
    except BaseException as exc:
        archive.unlink(missing_ok=True)  # Never upload an archive that failed its smoke check.
        detail = f'{type(exc).__name__}: {exc}'
        if (logs / 'smoke.log').exists():
            detail += '\n' + (logs / 'smoke.log').read_text(encoding='utf-8', errors='replace')[-8000:]
        print(detail, file=sys.stderr)
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            escaped = detail.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print(f'::error title=Desktop smoke test failed::{escaped}')
        raise
    with archive.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='utf-8')
    summary = {'version': version, 'target': target, 'archive': archive.name, 'sha256': checksum,
               'checks': ['extracted-app', 'pure-js-extension', 'http-health', 'app-assets', 'openapi'],
               'signing': 'ad-hoc only' if sys.platform == 'darwin' else 'unsigned'}
    (logs / 'smoke-result.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    if path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(path).open('a', encoding='utf-8') as stream:
            stream.write(f'### {target}\n\nPackaged `{archive.name}` and verified the extracted application. '
                         'Download the ZIP and SHA-256 file from this run’s artifacts.\n\n'
                         'Preview build: no release signing or notarization. Browser automation needs '
                         'a separately installed browser.\n')


if __name__ == '__main__':
    main()
