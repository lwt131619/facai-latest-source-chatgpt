"""The launcher must retain credentials and honor operator-supplied configuration."""
import importlib.util
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('facai_launcher', Path(__file__).parents[1]/'scripts/start.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_local_credentials_survive_restart_without_printing_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('FACAI_ADMIN_PASSWORD', raising=False)
    monkeypatch.delenv('FACAI_USERS_JSON', raising=False)
    first = launcher.credentials(tmp_path)
    assert len(first['FACAI_ADMIN_PASSWORD']) >= 24
    assert launcher.credentials(tmp_path) == first
    assert first['FACAI_ADMIN_PASSWORD'] not in capsys.readouterr().out
    if os.name != 'nt':
        assert (tmp_path/'local-owner.json').stat().st_mode & 0o077 == 0


def test_explicit_credentials_do_not_create_local_secret(tmp_path, monkeypatch):
    monkeypatch.setenv('FACAI_ADMIN_PASSWORD', 'externally-managed-credential')
    assert launcher.credentials(tmp_path) == {}
    assert not (tmp_path/'local-owner.json').exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file permissions')
def test_refuses_shared_local_credential_file(tmp_path, monkeypatch):
    monkeypatch.delenv('FACAI_ADMIN_PASSWORD', raising=False)
    monkeypatch.delenv('FACAI_USERS_JSON', raising=False)
    launcher.credentials(tmp_path)
    (tmp_path/'local-owner.json').chmod(0o644)
    with pytest.raises(ValueError, match='must be private'):
        launcher.credentials(tmp_path)
