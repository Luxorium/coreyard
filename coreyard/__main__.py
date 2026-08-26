"""``python -m coreyard`` — the same entry point the installed launcher runs.

``bin/coreyard`` is one ``exec`` onto this module, so there is no second place where a
command name is spelled and no way for the launcher and the package to disagree.
"""

from coreyard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
