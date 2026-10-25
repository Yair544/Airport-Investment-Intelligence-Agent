"""Operator commands: accounts, data refresh, deterministic analyses and backups.

Usage: python -m app.cli <command> [options]   (run from the backend/ directory)
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.security import hash_password
from app.storage import ConflictError, Storage, utcnow


def _storage(settings: Settings) -> Storage:
    storage = Storage(settings.database_path)
    storage.initialize()
    return storage


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            raise SystemExit("Passwords do not match.")
    if len(password) < 10:
        raise SystemExit("Use a password of at least 10 characters.")
    return password


# -- accounts ---------------------------------------------------------------------------------


def cmd_create_user(args: argparse.Namespace, settings: Settings) -> int:
    storage = _storage(settings)
    try:
        user_id = storage.create_user(args.username, hash_password(_read_password(args.password_stdin)))
    except ConflictError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Created user {args.username} ({user_id}).")
    return 0


def cmd_set_password(args: argparse.Namespace, settings: Settings) -> int:
    storage = _storage(settings)
    user = storage.get_user_by_username(args.username)
    if user is None:
        print("User not found.", file=sys.stderr)
        return 1
    storage.update_password(user.user_id, hash_password(_read_password(args.password_stdin)))
    print(f"Password updated for {args.username}.")
    return 0


def cmd_set_active(args: argparse.Namespace, settings: Settings, active: bool) -> int:
    storage = _storage(settings)
    user = storage.get_user_by_username(args.username)
    if user is None:
        print("User not found.", file=sys.stderr)
        return 1
    storage.set_user_active(user.user_id, active)
    print(f"User {args.username} {'enabled' if active else 'disabled (sessions revoked)'}.")
    return 0


# -- data refresh -----------------------------------------------------------------------------


def cmd_refresh(_: argparse.Namespace, settings: Settings) -> int:
    """Run one refresh job in the foreground (also the first-time data bootstrap)."""
    from app.analysis.service import compute_national_references
    from app.data.refresh import cleanup_incomplete_files, run_refresh_job

    storage = _storage(settings)
    cleanup_incomplete_files(storage)
    storage.interrupt_stale_refresh_jobs(utcnow(), include_queued=False)
    job, created = storage.create_or_get_active_refresh_job(utcnow())
    if not created:
        print(f"A refresh job is already active: {job.job_id} ({job.status}).", file=sys.stderr)
        return 1
    print(f"Running refresh job {job.job_id}. This can take a while on the first run.")
    run_refresh_job(
        storage,
        settings,
        lambda repo: compute_national_references(
            repo,
            horizon_years=settings.forecast_horizon_years,
            min_departures=settings.s3_min_departures,
        ),
        job_id=job.job_id,
    )
    finished = storage.get_refresh_job(job.job_id)
    if finished is None:
        return 1
    print(
        json.dumps(
            {
                "job_id": finished.job_id,
                "status": finished.status,
                "changed": finished.changed,
                "active_version_id": storage.get_active_data_version_id(),
                "sources": finished.sources,
                "error": finished.error,
            },
            indent=2,
            default=str,
        )
    )
    return 0 if finished.status == "completed" else 1


# -- deterministic analyses (no language model involved) -------------------------------------


def _resolve_ids(reference: Any, queries: Sequence[str]) -> tuple[list[str], str | None]:
    airport_ids: list[str] = []
    region_label: str | None = None
    for query in queries:
        outcome = reference.resolve(query)
        if outcome.status != "matched":
            options = ", ".join(f"{c.code} ({c.name})" for c in outcome.candidates)
            raise SystemExit(f"'{query}' is {outcome.status}" + (f": {options}" if options else "."))
        if outcome.region is not None or outcome.kind in ("region", "state"):
            region_label = outcome.region.name if outcome.region else query
        airport_ids.extend(a.airport_id for a in outcome.airports)
    return list(dict.fromkeys(airport_ids)), region_label


def cmd_analyze(args: argparse.Namespace, settings: Settings) -> int:
    from app.analysis.service import AnalysisService
    from app.data.resolution import load_reference_catalog
    from app.data.service import SqliteDataService

    reference = load_reference_catalog()
    analysis = AnalysisService(SqliteDataService(_storage(settings), reference), settings)
    ids, region_label = _resolve_ids(reference, args.airports)
    result: Any
    if args.analysis == "rank":
        result = analysis.rank_airports(
            ids, region_label=region_label, year=args.year, horizon_years=args.horizon
        )
    elif args.analysis == "compare":
        result = analysis.compare_airports(ids, year=args.year)
    elif args.analysis == "long-haul":
        result = analysis.long_haul_share(ids[0], year=args.year, threshold_miles=args.threshold)
    else:
        result = analysis.assess_unmet_demand(ids[0], year=args.year)
    print(result.model_dump_json(indent=2))
    return 0


# -- backup -----------------------------------------------------------------------------------


def cmd_backup(args: argparse.Namespace, settings: Settings) -> int:
    """Consistent database snapshot plus the source files it references, with a manifest."""
    storage = _storage(settings)
    destination = Path(args.dest).resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = destination / stamp
    target.mkdir(parents=True, exist_ok=False)
    storage.backup(target / "app.db")
    files = storage.list_data_files()
    copied = 0
    for item in files:
        source = settings.data_dir / item["local_path"]
        copy_to = destination / "raw" / item["local_path"]
        if source.is_file() and not copy_to.exists():
            copy_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copy_to)
            copied += 1
    manifest = {
        "created_at": stamp,
        "database": f"{stamp}/app.db",
        "active_data_version": storage.get_active_data_version_id(),
        "files": files,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(f"Backup written to {target} ({len(files)} source files referenced, {copied} newly copied).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("create-user", "Create an account (users can also sign up from the login screen)."),
        ("set-password", "Reset an account password."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("username")
        command.add_argument("--password-stdin", action="store_true", help="Read the password from stdin.")
    for name in ("disable-user", "enable-user"):
        commands.add_parser(name, help=f"{name.split('-')[0].title()} an account.").add_argument("username")

    commands.add_parser("refresh", help="Check sources and build/activate a new data version.")

    analyze = commands.add_parser("analyze", help="Run a deterministic analysis and print JSON.")
    analyze.add_argument("analysis", choices=["rank", "compare", "long-haul", "unmet"])
    analyze.add_argument("airports", nargs="+", help="Airport codes, names, states or a region.")
    analyze.add_argument("--year", type=int)
    analyze.add_argument("--horizon", type=int, help="Forecast horizon in years (rank).")
    analyze.add_argument("--threshold", type=float, help="Long-haul threshold in statute miles.")

    backup = commands.add_parser("backup", help="Snapshot the database and source files.")
    backup.add_argument("--dest", required=True, help="Backup directory on separate storage.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Results contain symbols such as "≥" and "→"; Windows consoles may default to a legacy codepage.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    settings = get_settings()
    handlers = {
        "create-user": lambda: cmd_create_user(args, settings),
        "set-password": lambda: cmd_set_password(args, settings),
        "disable-user": lambda: cmd_set_active(args, settings, False),
        "enable-user": lambda: cmd_set_active(args, settings, True),
        "refresh": lambda: cmd_refresh(args, settings),
        "analyze": lambda: cmd_analyze(args, settings),
        "backup": lambda: cmd_backup(args, settings),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
