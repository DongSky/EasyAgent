"""Pure WebAssembly JSON ABI. No WASI, imports, clock, files, network or credentials."""

import json
import sys
import wasmtime


def execute(body):
    config = wasmtime.Config()
    config.consume_fuel = True
    engine = wasmtime.Engine(config)
    module = wasmtime.Module(engine, body["source"])
    if module.imports:
        raise ValueError("pure WASM extensions cannot import host functions")
    store = wasmtime.Store(engine)
    store.set_limits(memory_size=body["memory_mb"] * 1024 * 1024, instances=1, memories=1, tables=1)
    store.set_fuel(10_000_000)
    instance = wasmtime.Instance(store, module, [])
    exports = instance.exports(store)
    memory = exports["memory"]
    data = json.dumps(body["request"]).encode()
    pointer = exports["alloc"](store, len(data))
    memory.write(store, data, pointer)
    packed = exports["handle"](store, pointer, len(data))
    start, length = (packed >> 32) & 0xFFFFFFFF, packed & 0xFFFFFFFF
    if length > 1_000_000 or start + length > memory.data_len(store):
        raise ValueError("WASM response is outside bounded memory")
    return json.loads(bytes(memory.read(store, start, start + length)))


if __name__ == "__main__":
    print(json.dumps(execute(json.loads(sys.stdin.buffer.readline(2_000_001)))))
