"""CoreYard — a salvage yard's inventory, published to Shopify and kept in step."""

# The one authoritative version. `pyproject.toml` reads it from here through
# setuptools' dynamic metadata, and `coreyard --version` prints it, so the package
# metadata, the CLI and the git tag cannot disagree — which they did while this said
# 1.0.0 and the release being prepared was v0.1.0. No 1.0.0 artifact was ever published
# (no tag, no release, no sdist/wheel), so the correction needs no migration note.
__version__ = "0.1.0"
