"""Export the active public data as a distributable snapshot; accounts and history are never copied.

Run from backend/: .venv/Scripts/python scripts/build_starter_data.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from app.bootstrap import SEED_DIR
from app.config import get_settings
from app.storage import SCHEMA_VERSION, Storage, to_iso, utcnow


def main() -> None:
    settings = get_settings()
    storage = Storage(settings.database_path)
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="starter-export-") as directory:
        database = Path(directory) / "starter.db"
        metadata = storage.export_seed(database)
        archive = Path(directory) / "aviation.db.gz"
        with (
            database.open("rb") as source,
            archive.open("wb") as output,
            gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as target,
        ):
            shutil.copyfileobj(source, target)
        with database.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": to_iso(utcnow()),
            **metadata,
            "database_bytes": database.stat().st_size,
            "database_sha256": digest,
            "archive_bytes": archive.stat().st_size,
            "contents": "Public FAA/BTS normalized data and provenance; raw archives are not bundled.",
        }
        shutil.copyfile(archive, SEED_DIR / "aviation.db.gz")
        (SEED_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
