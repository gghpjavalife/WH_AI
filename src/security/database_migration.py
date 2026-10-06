"""Explicit one-time migration from a plaintext SQLite file to SQLCipher."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import uuid4

import sqlcipher3.dbapi2 as sqlcipher

from core.config import settings
from services.database import (
    DatabaseSecurityError,
    _SQLITE_HEADER,
    connect_database,
    database_key_hex,
)


def migrate_plaintext_database(path: Path) -> None:
    database = path.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError("The configured SQLite database does not exist.")
    for suffix in ("-wal", "-shm", "-journal"):
        if database.with_name(database.name + suffix).exists():
            raise DatabaseSecurityError(
                "A database journal or WAL sidecar exists. Stop every app process "
                "and checkpoint/close the database before migration."
            )
    with database.open("rb") as source_file:
        header = source_file.read(len(_SQLITE_HEADER))
    if header != _SQLITE_HEADER:
        raise DatabaseSecurityError(
            "The database is not recognized as a plaintext SQLite file; refusing migration."
        )

    key_hex = database_key_hex()
    temporary = database.with_name(
        f".{database.name}.{uuid4().hex}.sqlcipher-tmp"
    )
    source = None
    failure: DatabaseSecurityError | None = None
    try:
        source = sqlcipher.connect(str(database))
        source.execute(
            f'ATTACH DATABASE ? AS encrypted KEY "x\'{key_hex}\'"',
            (str(temporary),),
        )
        source.execute("SELECT sqlcipher_export('encrypted')").fetchone()
        source.execute("DETACH DATABASE encrypted")
        source.close()
        source = None

        if os.name != "nt":
            os.chmod(temporary, 0o600)
        encrypted = connect_database(temporary)
        try:
            check = encrypted.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise DatabaseSecurityError(
                    "The migrated database did not pass SQLCipher integrity checks."
                )
        finally:
            encrypted.close()

        os.replace(temporary, database)
    except (OSError, sqlcipher.DatabaseError, DatabaseSecurityError):
        failure = DatabaseSecurityError(
            "Plaintext-to-SQLCipher migration failed. The original database was "
            "left unchanged; check the SQLCipher key and available disk space."
        )
    finally:
        if source is not None:
            try:
                source.close()
            except (OSError, sqlcipher.DatabaseError):
                if failure is None:
                    failure = DatabaseSecurityError(
                        "Could not close the plaintext database after migration."
                    )
                else:
                    failure = DatabaseSecurityError(
                        f"{failure} Closing the plaintext source also failed."
                    )
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                if failure is None:
                    failure = DatabaseSecurityError(
                        "Could not remove the temporary encrypted database; "
                        "inspect it before retrying."
                    )
                else:
                    failure = DatabaseSecurityError(
                        f"{failure} The temporary encrypted database could "
                        "not be removed."
                    )
    if failure is not None:
        raise failure from None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate the app's existing plaintext SQLite DB to SQLCipher."
    )
    parser.add_argument(
        "--confirm-plaintext-removal",
        action="store_true",
        help=(
            "Confirm the app is stopped and that the original plaintext database "
            "will be replaced (no plaintext backup is retained)."
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(settings.oauth_database_path),
        help="Database path; defaults to WEALTH_HOME_OAUTH_DB.",
    )
    args = parser.parse_args()
    if not args.confirm_plaintext_removal:
        parser.error(
            "stop the app and pass --confirm-plaintext-removal after making any "
            "required protected backup"
        )
    try:
        migrate_plaintext_database(args.database)
    except (OSError, DatabaseSecurityError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Database migrated to SQLCipher; the plaintext source was replaced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
