"""ARMS confirmatory-experiment shim.

The package is deliberately standard-library only.  It shells out to Git and
to the frozen repository test command; subject CLIs are isolated behind the
adapter interface in :mod:`adapters`.
"""

SCHEMA_VERSION = "arms-event-v1"

