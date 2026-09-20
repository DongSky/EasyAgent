"""Script entry point for the frozen desktop's bundled Python interpreter."""
import sys
import traceback


def main(source):
    # Windowed bundles may not initialize standard streams despite redirected handles.
    for name, descriptor in (("stdout", 1), ("stderr", 2)):
        stream = getattr(sys, name)
        if stream is None:
            stream = open(descriptor, "w", encoding="utf-8", closefd=False)
            setattr(sys, name, stream)
        else:
            stream.reconfigure(encoding="utf-8")
    sys.argv = ["-c"]
    sys.path.insert(0, "")  # Match python -c: scripts may import helpers in their workspace.
    try:
        exec(compile(source, "<easyagent-script>", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
    except Exception:
        # Avoid PyInstaller's windowed error dialog; errors belong in the invocation receipt.
        traceback.print_exc()
        raise SystemExit(1)
