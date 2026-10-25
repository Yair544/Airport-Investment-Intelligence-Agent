"""Shared test fixtures. Area-specific helpers live next to the tests that use them."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.storage import Storage


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        data_dir=tmp_path / "data",
        openai_api_key=None,
        allowed_origins=["http://testserver"],
    )


@pytest.fixture
def storage(settings: Settings) -> Storage:
    store = Storage(settings.database_path)
    store.initialize()
    return store
