"""``python -m foreman.v4.cli`` entry point.

Mirrors the console-script entry registered in pyproject.toml
(``foreman = "foreman.v4.cli:main"``). Having both forms means tests
can invoke the v4 CLI hermetically via ``[sys.executable, "-m",
"foreman.v4.cli", ...]`` without depending on the ``foreman`` script
being on PATH — useful on Windows / CI where script-shim resolution is
fragile, and for the Task 8.6 real-fork integration test.
"""

from __future__ import annotations

from foreman.v4.cli import main

if __name__ == "__main__":
    main()
