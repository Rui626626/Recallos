"""Format parsers.

Importing a module here registers its extensions with `ingest.base`. Parsers are
import-safe: an optional backend that is missing only fails when a file that
needs it is actually parsed, so `doctor` can report availability without
crashing.

Adding a format means adding one module and importing it below — nothing else in
the pipeline changes.
"""

from . import base, office, pdf, textual  # noqa: F401

__all__ = ["base", "office", "pdf", "textual"]
