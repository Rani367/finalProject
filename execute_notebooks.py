import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient


ROOT = Path(__file__).resolve().parent
NOTEBOOK_ROOT = ROOT / "TableToGraph"
TIMEOUT_SECONDS = 900
SETUP_SOURCE = """\
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

try:
    get_ipython().run_line_magic("matplotlib", "inline")
except NameError:
    pass
"""


def execute_notebook(path):
    notebook = nbformat.read(path, as_version=4)
    setup_cell = nbformat.v4.new_code_cell(SETUP_SOURCE)
    notebook.cells.insert(0, setup_cell)

    client = NotebookClient(
        notebook,
        timeout=TIMEOUT_SECONDS,
        kernel_name="python3",
        allow_errors=False,
    )
    client.execute()

    notebook.cells.pop(0)
    nbformat.write(notebook, path)


def main():
    failures = []
    notebooks = sorted(NOTEBOOK_ROOT.glob("Tevel*/*.ipynb"))
    for notebook in notebooks:
        print(f"Executing {notebook.relative_to(ROOT)}", flush=True)
        try:
            execute_notebook(notebook)
        except Exception as error:
            failures.append((notebook, error))
            print(f"Failed {notebook.relative_to(ROOT)}: {error}", file=sys.stderr, flush=True)

    if failures:
        print(
            f"Completed with {len(failures)} notebook failure(s); exporting existing embedded outputs.",
            file=sys.stderr,
            flush=True,
        )


if __name__ == "__main__":
    main()
