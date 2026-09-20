"""Disposable QuickJS worker: no host callbacks, filesystem, network or environment."""

import json
import sys

import quickjs


def main():
    request = json.loads(sys.stdin.buffer.readline(2_000_001))
    vm = quickjs.Context()
    vm.set_memory_limit(request["memory_mb"] * 1024 * 1024)
    vm.set_max_stack_size(512 * 1024)
    vm.set_time_limit(request["timeout_seconds"])
    vm.eval('"use strict";\n' + request["source"])
    argument = json.dumps(request["request"], ensure_ascii=True)
    raw = vm.eval("JSON.stringify(handle(" + argument + "))")
    if not isinstance(raw, str) or len(raw.encode()) > 1_000_000:
        raise ValueError("handler must return a bounded JSON value synchronously")
    json.loads(raw)
    sys.stdout.buffer.write((raw + "\n").encode('utf-8'))


if __name__ == "__main__":
    main()
