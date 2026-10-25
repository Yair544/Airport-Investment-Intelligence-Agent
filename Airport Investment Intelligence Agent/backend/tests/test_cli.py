"""Account commands accept passwords piped from Windows and Unix shells."""

from __future__ import annotations

import io
import sys

import pytest

from app import cli
from app.config import Settings
from app.security import hash_password, verify_password
from app.storage import Storage


@pytest.mark.parametrize("command", ["create-user", "set-password"])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_password_stdin_accepts_shell_line_endings(
    command: str, line_ending: str, settings: Settings, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    if command == "set-password":
        storage.create_user("demo", hash_password("previous-password"))
    password = "AirportDemo123!"
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + line_ending))
    assert cli.main([command, "demo", "--password-stdin"]) == 0
    user = storage.get_user_by_username("demo")
    assert user is not None and verify_password(user.password_hash, password)
