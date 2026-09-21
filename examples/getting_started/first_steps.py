"""Run from the repository root: uv run python examples/getting_started/first_steps.py.

Two ordinary Python functions become a saved, inspectable workflow. No server,
model connection, network request, or API key is needed.
"""

from easyagent import Runtime, Sequential, node


@node
def clean(text: str) -> str:
    return text.strip()


@node
def count_words(text: str) -> dict:
    return {"text": text, "words": len(text.split())}


def main():
    workflow = Sequential(clean, count_words)
    with Runtime(database=".eah/first-steps.db") as runtime:
        result = runtime.run(workflow, "  Hello EasyAgent  ")
        print(result.value)
        workflow.export(".eah/first-steps.workflow.json")


if __name__ == "__main__":
    main()
