"""Entry point: `python -m decision_mail orbit-tool` serves one Orbit exec call."""

from __future__ import annotations

import sys

from decision_mail.protocol import serve


def main(argv: list[str]) -> int:
    if argv != ["orbit-tool"]:
        print("usage: python -m decision_mail orbit-tool", file=sys.stderr)
        return 2
    sys.stdout.write(serve(sys.stdin.read()))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
