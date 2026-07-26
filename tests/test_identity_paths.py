import json
import os
import tempfile
import unittest
from unittest.mock import patch

from clients.shared.identity import MEPIdentity
from clients.shared import identity_paths
from node.identity import MEPIdentity as RuntimeMEPIdentity


class TestIdentityPaths(unittest.TestCase):
    def test_shared_identity_reuses_runtime_x25519_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, "persistent-bot.pem")
            runtime_identity = RuntimeMEPIdentity(key_path)

            shared_identity = MEPIdentity(key_path)

            self.assertEqual(shared_identity.node_id, runtime_identity.node_id)
            self.assertEqual(
                shared_identity.x25519_public_key,
                runtime_identity.x25519_public_key,
            )
            self.assertTrue(os.path.exists(key_path.replace(".pem", "_enc.pem")))
            self.assertFalse(os.path.exists(f"{key_path}.x25519.pem"))

    def test_shared_identity_creates_runtime_compatible_x25519_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, "new-shared-bot.pem")

            MEPIdentity(key_path)

            self.assertTrue(os.path.exists(key_path.replace(".pem", "_enc.pem")))
            self.assertFalse(os.path.exists(f"{key_path}.x25519.pem"))

    def test_runtime_identity_reuses_existing_modern_x25519_sidecar(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, "modern-sidecar-bot.pem")
            shared_identity = MEPIdentity(key_path)
            legacy_path = key_path.replace(".pem", "_enc.pem")
            modern_path = f"{key_path}.x25519.pem"
            os.replace(legacy_path, modern_path)

            runtime_identity = RuntimeMEPIdentity(key_path)

            self.assertEqual(
                runtime_identity.x25519_public_key,
                shared_identity.x25519_public_key,
            )
            self.assertEqual(runtime_identity.enc_key_path, modern_path)
            self.assertFalse(os.path.exists(legacy_path))

    def test_both_identity_classes_warn_and_select_legacy_when_sidecars_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, "conflicting-sidecars-bot.pem")
            legacy_identity = MEPIdentity(key_path)
            modern_source_path = os.path.join(tmpdir, "modern-source.pem")
            MEPIdentity(modern_source_path)
            modern_path = f"{key_path}.x25519.pem"
            os.replace(
                modern_source_path.replace(".pem", "_enc.pem"),
                modern_path,
            )

            with self.assertWarnsRegex(RuntimeWarning, "Both legacy and modern X25519 sidecars exist"):
                shared_identity = MEPIdentity(key_path)
            with self.assertWarnsRegex(RuntimeWarning, "Both legacy and modern X25519 sidecars exist"):
                runtime_identity = RuntimeMEPIdentity(key_path)

            self.assertEqual(
                shared_identity.x25519_public_key,
                legacy_identity.x25519_public_key,
            )
            self.assertEqual(
                runtime_identity.x25519_public_key,
                legacy_identity.x25519_public_key,
            )
            self.assertEqual(
                runtime_identity.enc_key_path,
                key_path.replace(".pem", "_enc.pem"),
            )

    def test_default_key_dir_uses_shared_home_resolver(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=True):
                with patch("clients.shared.identity_paths._user_home_dir", return_value=tmpdir):
                    self.assertEqual(identity_paths.default_key_dir(), os.path.join(tmpdir, ".mep"))

    def test_resolve_identity_key_path_creates_stable_home_identity_and_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"MEP_KEY_DIR": tmpdir}, clear=False):
                key_path = identity_paths.resolve_identity_key_path(
                    alias_hint="alpha-bot",
                    create_if_missing=True,
                )

            self.assertTrue(key_path.startswith(tmpdir))
            self.assertTrue(os.path.exists(key_path))
            self.assertEqual(identity_paths.read_alias_sidecar(key_path), "alpha-bot")
            with open(os.path.join(tmpdir, "bots.json"), "r", encoding="utf-8") as handle:
                registry = json.load(handle)
            self.assertEqual(registry["aliases"]["alpha-bot"]["key_path"], key_path)

    def test_resolve_identity_key_path_creates_new_alias_when_store_has_other_bot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"MEP_KEY_DIR": tmpdir}, clear=False):
                alpha_key = identity_paths.resolve_identity_key_path(
                    alias_hint="alpha-bot",
                    create_if_missing=True,
                )
                beta_key = identity_paths.resolve_identity_key_path(
                    alias_hint="beta-bot",
                    create_if_missing=True,
                )

            self.assertNotEqual(os.path.abspath(alpha_key), os.path.abspath(beta_key))
            self.assertEqual(identity_paths.read_alias_sidecar(alpha_key), "alpha-bot")
            self.assertEqual(identity_paths.read_alias_sidecar(beta_key), "beta-bot")
            with open(os.path.join(tmpdir, "bots.json"), "r", encoding="utf-8") as handle:
                registry = json.load(handle)
            self.assertEqual(registry["aliases"]["alpha-bot"]["key_path"], alpha_key)
            self.assertEqual(registry["aliases"]["beta-bot"]["key_path"], beta_key)

    def test_resolve_identity_key_path_recovers_registered_alias_across_worktrees(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_local = os.path.join(tmpdir, "repo_a", ".mep")
            os.makedirs(repo_local, exist_ok=True)
            key_path = os.path.join(repo_local, "auditor.pem")
            MEPIdentity(key_path)
            with patch.dict(os.environ, {"MEP_KEY_DIR": os.path.join(tmpdir, "home_store")}, clear=False):
                identity_paths.remember_identity(key_path, "Trae Repo Auditor")
                recovered = identity_paths.resolve_identity_key_path(alias_hint="Trae Repo Auditor")

            self.assertEqual(os.path.abspath(recovered), os.path.abspath(key_path))

    def test_resolve_identity_key_path_recovers_legacy_repo_local_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = os.path.join(tmpdir, "repo")
            nested = os.path.join(repo_root, "node")
            os.makedirs(nested, exist_ok=True)
            os.makedirs(os.path.join(repo_root, ".git"), exist_ok=True)
            legacy_dir = os.path.join(repo_root, ".mep")
            os.makedirs(legacy_dir, exist_ok=True)
            key_path = os.path.join(legacy_dir, "legacy.pem")
            identity = MEPIdentity(key_path)
            identity_paths.write_alias_sidecar(key_path, "legacy-bot")

            with patch.dict(os.environ, {}, clear=True):
                with patch("os.getcwd", return_value=nested):
                    recovered = identity_paths.resolve_identity_key_path(
                        alias_hint="legacy-bot",
                        start_path=nested,
                    )

            self.assertEqual(
                os.path.abspath(recovered),
                os.path.abspath(os.path.join(legacy_dir, f"{identity.node_id}.pem")),
            )
