"""Provision the local SQLCipher key in the active OS credential vault."""

from __future__ import annotations

import argparse
import sys

from services.database import (
    DatabaseKeyUnavailable,
    database_key_hex,
    provision_database_key,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision or check the local SQLCipher database key."
    )
    parser.add_argument(
        "action",
        choices=("provision", "check"),
        help="Provision a random key in the OS vault, or check that it can be read.",
    )
    args = parser.parse_args()
    try:
        if args.action == "provision":
            provision_database_key()
            print("SQLCipher key provisioned in the native OS credential vault.")
        else:
            database_key_hex()
            print("SQLCipher key is available.")
    except DatabaseKeyUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
