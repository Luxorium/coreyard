"""The offline suite runs against a scratch data root, never the installation's.

Without this, a test that calls the CLI in-process resolves `DATA_ROOT` the way any run
does — to this checkout, because rule 2 says a writable checkout is its own data home — and
writes into the live sync-state database. That is how `sync delta` came to be recorded as
having failed every time the suite ran: `cli` records refusals in the run history, on
purpose, so a scheduled job that starts refusing does not read as one nobody scheduled. The
history is the operator's evidence that the pipeline is healthy, and the tests were filing
failures into it.

`COREYARD_HOME` is set here rather than per-test because `DATA_ROOT` is resolved once, when
`coreyard.config` is imported: by the time a test module runs, every path is already
decided. This package is imported before any of them, which is the only moment early enough.
"""

import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="coreyard-tests-")
os.environ["COREYARD_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, True)
