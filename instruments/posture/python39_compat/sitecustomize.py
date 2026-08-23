"""Restore the one Python 3.9 stdlib alias required by historical Click tests.

The selected control base predates the removal of ``collections.Iterable`` in
Python 3.10.  The pilot itself runs on Python 3.12, so every prepared base and
every arm receives this same, deliberately narrow compatibility layer.
"""

import collections
import collections.abc


if not hasattr(collections, "Iterable"):
    collections.Iterable = collections.abc.Iterable  # type: ignore[attr-defined]
