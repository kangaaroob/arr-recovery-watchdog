"""Allow running the service with `python -m arr_recovery_watchdog`."""

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
