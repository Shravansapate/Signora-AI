"""Offline restore and snapshot-consistent PostgreSQL/immutable-object backups.

Run with ``python -m app.recovery --help``. This is an administrator tool, never an
HTTP endpoint. Restore only trusted backups into an empty, offline database.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.engine import make_url

from app.config import Settings
from app.storage import MAX_OBJECT_BYTES

SCHEMA = "0010_display_credentials"
CATALOG_LOCK = "1397311310, 1"
INVENTORY = """SELECT DISTINCT storage_key,sha256,size_bytes FROM motion_versions
WHERE deleted_at IS NULL ORDER BY storage_key"""
ENV_KEYS = {
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "channel_binding": "PGCHANNELBINDING",
    "application_name": "PGAPPNAME",
    "options": "PGOPTIONS",
}


class RecoveryError(ValueError):
    pass


class Digest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, strict=True)


class Asset(Digest):
    storage_key: str
    size_bytes: int = Field(gt=0, le=MAX_OBJECT_BYTES, strict=True)

    @model_validator(mode="after")
    def canonical_key(self):
        if self.storage_key != f"sha256/{self.sha256[:2]}/{self.sha256}.glb":
            raise ValueError("Asset key must match its content digest")
        return self


class Backup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    schema_revision: Literal[
        "0006_live_delivery",
        "0007_library_workflow",
        "0008_library_reupload",
        "0009_display_routing",
        "0010_display_credentials",
    ] = SCHEMA
    backup_id: UUID
    created_at: AwareDatetime
    postgres_major: int = Field(ge=14, le=99, strict=True)
    extensions: dict[str, str]
    database: Digest
    assets: list[Asset]

    @model_validator(mode="after")
    def unique_objects(self):
        keys = [item.storage_key for item in self.assets]
        if keys != sorted(set(keys)):
            raise ValueError("Asset inventory must be unique and sorted")
        return self


def connection_uri(database_url):
    url = make_url(database_url)
    if url.drivername not in {"postgresql", "postgresql+psycopg"} or not url.database:
        raise RecoveryError("An explicit PostgreSQL database is required")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _environment(uri):
    parameters = conninfo_to_dict(uri)
    if parameters.keys() - ENV_KEYS.keys():
        raise RecoveryError("Unsupported recovery connection option")
    # Never inherit a different libpq destination/service or expose credentials in argv.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PG", "SIGNORA_"))}
    env.update({ENV_KEYS[k]: v for k, v in parameters.items()})
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    env["PGAPPNAME"] = "signora-recovery"
    return env


def _run(pg_bin, program, arguments, uri, *, timeout=600):
    binary = pg_bin / (program + (".exe" if os.name == "nt" else ""))
    try:
        result = subprocess.run(
            [str(binary), *arguments],
            env=_environment(uri),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"{program} unavailable or timed out") from exc
    if result.returncode:
        # Database diagnostics may contain source text, hostnames or credentials.
        raise RecoveryError(f"{program} failed (exit {result.returncode}); target remains offline")
    return result.stdout.decode("utf-8", errors="replace")


def _major(connection, pg_bin, uri, programs):
    major = connection.info.server_version // 10000
    for program in programs:
        version = _run(pg_bin, program, ["--version"], uri, timeout=15)
        match = re.search(r"\(PostgreSQL\) (\d+)\.", version)
        if not match or int(match[1]) != major:
            raise RecoveryError("Recovery tools must match the PostgreSQL server major version")
    return major


def _digest(path, destination=None):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        if destination is None:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            _sync_directory(destination.parent)
    return Digest(sha256=digest.hexdigest(), size_bytes=size)


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _sync_tree(root):
    # Persist newly created ancestor entries too, not just the leaf object directory.
    if os.name != "nt":
        for current, _, _ in os.walk(root, topdown=False):
            _sync_directory(Path(current))
        _sync_directory(root.parent)


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    _sync_directory(path.parent)


def _contained(root, key):
    path = (root / key).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise RecoveryError("Backup or storage path escapes its root")
    return path


def _check(path, expected, destination=None):
    actual = _digest(path, destination)
    if actual.sha256 != expected.sha256 or actual.size_bytes != expected.size_bytes:
        raise RecoveryError("Backup or immutable asset checksum/size mismatch")


def _inventory(connection):
    with connection.cursor(row_factory=dict_row) as cursor:
        return [Asset.model_validate(row) for row in cursor.execute(INVENTORY)]


def create_backup(database_url, storage_root: Path, destination: Path, pg_bin: Path):
    uri = connection_uri(database_url)
    storage_root, destination = storage_root.resolve(strict=True), destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise RecoveryError("Backup destination must not exist")
    if destination.resolve().is_relative_to(storage_root):
        raise RecoveryError("Backup must be outside immutable asset storage")
    backup_id = uuid4()
    staging = destination.with_name(destination.name + f".partial-{backup_id}")
    # Completion is published only by the final directory rename. Preserve failed evidence.
    staging.mkdir(mode=0o700)
    with psycopg.connect(uri, autocommit=True, connect_timeout=10) as connection:
        connection.execute("SET lock_timeout='30s'")
        connection.execute("SET statement_timeout='600s'")
        major = _major(connection, pg_bin, uri, ["pg_dump"])
        # Acquire BEFORE the RR snapshot: a cleanup that just finished must be visible.
        connection.execute(f"SELECT pg_advisory_lock_shared({CATALOG_LOCK})")
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if connection.execute("SELECT version_num FROM alembic_version").fetchall() != [
                (SCHEMA,)
            ]:
                raise RecoveryError("Unsupported database schema")
            snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
            snapshot_at = datetime.now(UTC)
            assets = _inventory(connection)
            extensions = dict(connection.execute("SELECT extname,extversion FROM pg_extension"))
            _run(
                pg_bin,
                "pg_dump",
                [
                    "--no-password",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--snapshot={snapshot}",
                    "--file",
                    str(staging / "database.dump"),
                ],
                uri,
            )
            with (staging / "database.dump").open("r+b") as dump:
                os.fsync(dump.fileno())
            for asset in assets:
                _check(
                    _contained(storage_root, asset.storage_key),
                    asset,
                    staging / "assets" / asset.storage_key,
                )
            backup = Backup(
                backup_id=backup_id,
                created_at=snapshot_at,
                postgres_major=major,
                extensions=extensions,
                database=_digest(staging / "database.dump"),
                assets=assets,
            )
            _write_json(staging / "manifest.json", backup.model_dump(mode="json"))
            _sync_tree(staging)
        # Closing the dedicated connection releases the shared session lock on all paths.
    # POSIX rename can replace an empty directory: refuse it explicitly as well.
    if destination.exists():
        raise RecoveryError("Backup destination appeared during backup")
    staging.rename(destination)
    _sync_directory(destination.parent)
    return backup


def verify_backup(directory: Path):
    directory = directory.resolve(strict=True)
    manifest = _contained(directory, "manifest.json")
    if manifest.stat().st_size > 16 * 1024 * 1024:
        raise RecoveryError("Backup manifest exceeds 16 MiB")
    backup = Backup.model_validate_json(manifest.read_bytes())
    _check(_contained(directory, "database.dump"), backup.database)
    for asset in backup.assets:
        _check(_contained(directory, "assets/" + asset.storage_key), asset)
    return backup


def _empty_database(connection):
    occupied = connection.execute("""SELECT EXISTS (
      SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public','information_schema')
        AND nspname NOT LIKE 'pg_%'
      UNION ALL SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public'
      UNION ALL SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public'
      UNION ALL SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname='public'
      UNION ALL SELECT 1 FROM pg_extension WHERE extname<>'plpgsql')""").fetchone()[0]
    if occupied:
        raise RecoveryError("Restore requires an empty database; nothing will be overwritten")


def _fence_sql(backup):
    expected = json.dumps([a.model_dump() for a in backup.assets])
    # All literals originate from the validated manifest; quote even those values.
    return (
        psycopg.sql.SQL("""
SET search_path = public, pg_catalog;
DO $recovery$
BEGIN
  IF (SELECT array_agg(version_num::text) FROM alembic_version)
      IS DISTINCT FROM ARRAY[{schema}] THEN
    RAISE EXCEPTION 'Recovery schema mismatch';
  END IF;
  IF (SELECT jsonb_object_agg(extname,extversion) FROM pg_extension)
      IS DISTINCT FROM {extensions}::jsonb THEN
    RAISE EXCEPTION 'Recovery extension mismatch';
  END IF;
  IF (SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY storage_key),'[]'::jsonb)
      FROM ({inventory}) a) IS DISTINCT FROM {expected}::jsonb THEN
    RAISE EXCEPTION 'Recovery asset inventory mismatch';
  END IF;
END $recovery$;
SELECT append_station_event(station_id,id,current_revision,'WITHDRAWN')
  FROM announcements WHERE state='LIVE' ORDER BY station_id,id;
UPDATE announcements SET state='WITHDRAWN' WHERE state='LIVE';
UPDATE display_devices SET enabled=false,revision=revision+1,session_id=NULL,lease_until=NULL;
INSERT INTO admin_audit_logs(id,actor,action,entity_id,details) VALUES
  (gen_random_uuid(),'signora-recovery','BACKUP_RESTORED',{backup_id},
   jsonb_build_object('backup_id',{backup_id}::text,'restored_at',now(),
     'live_messages_withdrawn',true,'display_registrations_disabled',true));
""")
        .format(
            schema=psycopg.sql.Literal(backup.schema_revision),
            inventory=psycopg.sql.SQL(INVENTORY),
            expected=psycopg.sql.Literal(expected),
            extensions=psycopg.sql.Literal(json.dumps(backup.extensions)),
            backup_id=psycopg.sql.Literal(str(backup.backup_id)),
        )
        .as_string()
    )


def restore_backup(directory: Path, database_url, storage_root: Path, pg_bin: Path):
    backup = verify_backup(directory)
    uri = connection_uri(database_url)
    storage_root = storage_root.absolute()
    if storage_root.exists() or storage_root.is_symlink():
        raise RecoveryError("Restore asset destination must not exist")
    if storage_root.resolve().is_relative_to(directory.resolve()):
        raise RecoveryError("Restore assets must be outside the backup")
    with psycopg.connect(uri, autocommit=True, connect_timeout=10) as connection:
        if not connection.execute("SELECT pg_try_advisory_lock(1397311310, 8)").fetchone()[0]:
            raise RecoveryError("Another restore is already running")
        if _major(connection, pg_bin, uri, ["pg_restore", "psql"]) != backup.postgres_major:
            raise RecoveryError("Restore requires the backup's PostgreSQL major version")
        _empty_database(connection)
        for name, version in backup.extensions.items():
            if not connection.execute(
                "SELECT 1 FROM pg_available_extension_versions WHERE name=%s AND version=%s",
                (name, version),
            ).fetchone():
                raise RecoveryError("A backup extension version is unavailable on the target")
        storage_root.mkdir(mode=0o700)
        for asset in backup.assets:
            _check(
                _contained(directory, "assets/" + asset.storage_key),
                asset,
                storage_root / asset.storage_key,
            )
        _sync_tree(storage_root)
        # psql wraps BOTH the restored schema/data and the operational fence in one
        # transaction. An interrupted restore cannot leave old live messages committed.
        with tempfile.TemporaryDirectory(prefix="signora-restore-", dir=storage_root) as work:
            sql_file, fence_file = Path(work) / "database.sql", Path(work) / "fence.sql"
            _run(
                pg_bin,
                "pg_restore",
                [
                    "--no-owner",
                    "--no-privileges",
                    "--file",
                    str(sql_file),
                    str(directory.resolve() / "database.dump"),
                ],
                uri,
            )
            fence_file.write_text(_fence_sql(backup), encoding="utf-8")
            _empty_database(connection)
            _run(
                pg_bin,
                "psql",
                [
                    "--no-psqlrc",
                    "--no-password",
                    "--single-transaction",
                    "--set=ON_ERROR_STOP=on",
                    "--file",
                    str(sql_file),
                    "--file",
                    str(fence_file),
                ],
                uri,
            )
        if _inventory(connection) != backup.assets:
            raise RecoveryError("Restored inventory changed; leave services offline")
        receipt = {
            "backup_id": str(backup.backup_id),
            "restored_at": datetime.now(UTC).isoformat(),
            "schema_revision": backup.schema_revision,
            "assets_verified": len(backup.assets),
            "live_messages_withdrawn": True,
            "display_registrations_disabled": True,
        }
        _write_json(storage_root / "restore-receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("backup", "verify", "restore"):
        command = sub.add_parser(name)
        command.add_argument("directory", type=Path)
        if name != "verify":
            command.add_argument("--pg-bin", type=Path, required=True)
        if name == "restore":
            command.add_argument("--storage-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            settings = Settings()
            result = create_backup(
                settings.database_url.get_secret_value(),
                settings.storage_root,
                args.directory,
                args.pg_bin,
            ).model_dump(mode="json")
        elif args.operation == "restore":
            target = os.environ.get("SIGNORA_RESTORE_DATABASE_URL")
            if not target:
                raise RecoveryError("Set SIGNORA_RESTORE_DATABASE_URL to an empty offline target")
            result = restore_backup(args.directory, target, args.storage_root, args.pg_bin)
        else:
            result = verify_backup(args.directory).model_dump(mode="json")
        print(json.dumps(result, indent=2))
    except (RecoveryError, ValidationError, OSError, psycopg.Error, ValueError) as exc:
        # Validation/DB errors may embed secrets. Print only deliberately safe diagnostics.
        message = (
            str(exc) if type(exc) is RecoveryError else "Recovery failed: " + type(exc).__name__
        )
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
