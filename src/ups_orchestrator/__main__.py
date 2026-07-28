"""Allow `python -m ups_orchestrator <event> [upsname]`."""

import sys

from ups_orchestrator.cli import main

if __name__ == "__main__":
    # main()'s return value IS the exit code, and dropping it made this entry
    # point always exit 0 while the console script exited correctly. The two
    # disagreed silently: `monitor verify` printed DISARMED and exited 0 here, so
    # anything keying on the code — including the phase's own UAT, which aliases
    # `uo` to this form — read "armed" from a command that had just said the
    # opposite.
    sys.exit(main(sys.argv[1:]))
