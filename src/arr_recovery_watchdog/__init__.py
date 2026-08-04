"""Watchdog that clears Arr indexer cooldowns after a Gluetun VPN outage recovers."""

from importlib.metadata import PackageNotFoundError, version

# The single source of truth for the version is [project].version in
# pyproject.toml; the release workflow refuses to publish a tag that does not
# match it. Reading the installed metadata here keeps the two in lockstep.
try:
    __version__ = version("arr-recovery-watchdog")
except PackageNotFoundError:
    # Running from a source tree that was never pip-installed.
    __version__ = "0.0.0+unknown"
