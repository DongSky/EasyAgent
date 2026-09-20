# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0
"""Run after setup.py. Use the same EAH_DEMO_KEY to resume, including across languages."""
import json
import os
from pathlib import Path
import sys

from easyagent_client import Client, RunStopped


def main(reference, output):
    key = os.environ.get('EAH_DEMO_KEY', 'media-demo')
    with Client() as client:
        generate = client.workflow('media.expression_video', key=key, timeout=1900)
        result = generate(reference_image=Path(reference))
        result.download('image', output / 'expression.png')
        result.download('video', output / 'animation.mp4')
        print(json.dumps({'language': 'Python', 'run_id': result.id, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python python_demo.py reference.png [output-directory]')
    try:
        main(sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else 'output/media-sdk/python'))
    except RunStopped as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
