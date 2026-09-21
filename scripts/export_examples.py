"""Export saved workflows into examples/workflows/ as importable, credential-free packages.

A published example must be something a reader can actually import: this script runs the
real exporter, refuses to write a package that still needs a credential, strips anything
workspace-specific, and writes the Apache-2.0 licence file beside it (the convention the
existing shared example follows). It never invents a workflow or edits src/.

    uv run python scripts/export_examples.py --list
    uv run python scripts/export_examples.py <workflow-id> --id example.<name> --title "显示名称"
    uv run python scripts/export_examples.py <workflow-id> --id example.<name> --check    # dry run

Exit status is 1 if any requested export could not be produced.

SPDX-FileCopyrightText: 2026 EasyAgent contributors
SPDX-License-Identifier: AGPL-3.0-only
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from easyagent.runtime import Hub  # noqa: E402
from easyagent.workflow_packages import export_workflow, preview_import  # noqa: E402

TARGET = Path(__file__).resolve().parents[1] / 'examples' / 'workflows'
LICENCE = """SPDX-FileCopyrightText: 2026 EasyAgent contributors
SPDX-License-Identifier: Apache-2.0
"""


# Tied to the machine that produced the package. The importer rebinds models, credentials
# and knowledge namespaces, so keeping the originals would only leak where this ran.
LOCAL_KEYS = ('backends', 'extensions', 'voice_settings', 'policy_versions', 'goal_original_writes',
              'goal_reused_writes', 'workspace_conversation', 'workspace_turn', 'created_by')


def strip_local_details(package):
    """Return a WorkflowPackage with workspace-specific metadata removed and the digest recomputed."""
    from easyagent.components import digest
    from easyagent.workflow_packages import WorkflowPackage

    body = package.model_dump(exclude={'digest'})
    workflow = dict(body['workflow'])
    metadata = {k: v for k, v in (workflow.get('metadata') or {}).items() if k not in LOCAL_KEYS}
    workflow['metadata'] = metadata
    body['workflow'] = workflow
    return WorkflowPackage(**body, digest=digest(body))


def credentials_required(package):
    return [name for name in (package.requirements or {}).get('credentials', []) if name]


def main(args):
    hub = Hub(args.database)
    if args.list:
        for saved in hub.development.workflows():
            print(f"{saved['id']}@{saved['revision']}  {saved['workflow']['name']}")
        return

    if not args.workflow:
        raise SystemExit('give a workflow id, or --list to see what is saved')
    saved = hub.development.get('workflow', args.workflow, args.revision)
    package = export_workflow(hub, saved['workflow'], {'id': args.id, 'revision': saved['revision']})
    package = strip_local_details(package)

    missing = credentials_required(package)
    if missing:
        raise SystemExit('refusing to export: the workflow still needs credentials '
                         + ', '.join(missing) + ' — share the shape, not the secret')
    serialised = package.model_dump()
    serialised['source'] = {'id': args.id, 'revision': saved['revision']}
    body = {k: v for k, v in serialised.items() if k != 'digest'}
    from easyagent.components import digest

    serialised['digest'] = digest(body)
    package = package.__class__(**serialised)
    # Round-trip through the real importer so a broken package never reaches examples/.
    preview = preview_import(hub, {'package': package})
    if preview.get('missing'):
        raise SystemExit('refusing to export, the package is not importable here: '
                         + '; '.join(preview['missing']))

    TARGET.mkdir(parents=True, exist_ok=True)
    path = TARGET / f"{args.id.split('.', 1)[-1]}.eah-workflow.json"
    if args.check:
        print(f'would write {path} ({len(json.dumps(package.model_dump(), ensure_ascii=False))} bytes)')
        print(json.dumps(preview, ensure_ascii=False, indent=2)[:2000])
        return
    path.write_text(json.dumps(package.model_dump(), ensure_ascii=False, indent=2), encoding='utf-8')
    Path(str(path) + '.license').write_text(LICENCE, encoding='utf-8')
    print(f'wrote {path}')
    print(json.dumps(preview, ensure_ascii=False, indent=2)[:2000])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workflow', nargs='?', help='saved workflow id to export')
    parser.add_argument('--revision', type=int, default=None)
    parser.add_argument('--id', default=None, help='package id, e.g. example.notice-actions')
    parser.add_argument('--title', default='', help='human title recorded in the package preview')
    parser.add_argument('--database', default='.eah/hub.db')
    parser.add_argument('--list', action='store_true', help='list saved workflows and exit')
    parser.add_argument('--check', action='store_true', help='validate and print, write nothing')
    arguments = parser.parse_args()
    if arguments.id is None:
        arguments.id = 'example.' + (arguments.workflow or 'unknown').replace('_', '-')
    main(arguments)
