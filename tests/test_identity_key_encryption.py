import warnings

import pytest

from clients.shared.identity import MEPIdentity


def test_creates_encrypted_key_when_password_is_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEP_IDENTITY_KEY_PASSWORD", "super-secret-pass")
    key_path = tmp_path / "identity.pem"

    identity = MEPIdentity(str(key_path))

    pem = key_path.read_bytes()
    assert b"ENCRYPTED" in pem
    assert identity.node_id.startswith("node_")


def test_loading_encrypted_key_requires_password(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "identity.pem"
    monkeypatch.setenv("MEP_IDENTITY_KEY_PASSWORD", "super-secret-pass")
    MEPIdentity(str(key_path))

    monkeypatch.delenv("MEP_IDENTITY_KEY_PASSWORD", raising=False)
    monkeypatch.delenv("MEP_KEY_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="Encrypted MEP identity key requires"):
        MEPIdentity(str(key_path))


def test_legacy_unencrypted_key_still_loads_with_warning(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "identity.pem"
    monkeypatch.delenv("MEP_IDENTITY_KEY_PASSWORD", raising=False)
    monkeypatch.delenv("MEP_KEY_PASSWORD", raising=False)
    MEPIdentity(str(key_path))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        MEPIdentity(str(key_path))

    assert any("stored unencrypted at rest" in str(item.message) for item in caught)
