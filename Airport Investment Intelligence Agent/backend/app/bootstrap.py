"""One-time installation of bundled public aviation data and a local demonstration account."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from app.security import hash_password
from app.storage import ConflictError, Storage

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "AirportDemo123!"
SEED_DIR = Path(__file__).parent / "data" / "starter"
logger = logging.getLogger("app.bootstrap")


@contextmanager
def _starter_database(storage: Storage, seed_dir: Path) -> Iterator[Path]:
    manifest = json.loads((seed_dir / "manifest.json").read_text(encoding="utf-8"))
    logger.info("starter_data_loading", extra={"version_id": manifest["version_id"]})
    # A failed/crashed extraction can never become the live database. The import itself is atomic.
    with TemporaryDirectory(prefix="starter-") as directory:
        extracted = Path(directory) / "starter.db"
        digest = hashlib.sha256()
        size = 0
        with gzip.open(seed_dir / "aviation.db.gz", "rb") as source, extracted.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > manifest["database_bytes"]:
                    raise ValueError("Starter snapshot exceeds its declared size.")
                digest.update(chunk)
                target.write(chunk)
        if size != manifest["database_bytes"] or digest.hexdigest() != manifest["database_sha256"]:
            raise ValueError("Starter snapshot is damaged. Rebuild the image from the project files.")
        yield extracted


def bootstrap(storage: Storage, seed_dir: Path = SEED_DIR) -> None:
    """Install only missing data. Fail startup cleanly rather than serve a partial installation."""
    storage.database_path.parent.mkdir(parents=True, exist_ok=True)
    if not storage.database_path.exists():
        with _starter_database(storage, seed_dir) as extracted:
            storage.restore_seed_if_missing(extracted)
    storage.initialize()
    if storage.get_active_data_version_id() is None:
        with _starter_database(storage, seed_dir) as extracted:
            storage.import_seed(extracted)
    logger.info("starter_data_ready", extra={"version_id": storage.get_active_data_version_id()})

    if storage.get_user_by_username(DEMO_USERNAME) is None:
        # Another startup may have created it already. Never reset a pre-existing password.
        with suppress(ConflictError):
            storage.create_user(DEMO_USERNAME, hash_password(DEMO_PASSWORD))
        logger.info("demo_user_ready")
