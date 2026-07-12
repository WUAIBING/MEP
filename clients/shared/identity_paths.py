import hashlib
import json
import os
import time
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from clients.shared.identity import MEPIdentity

LEGACY_RUNTIME_KEY_NAME = "mep_runtime.pem"
IDENTITY_REGISTRY_NAME = "bots.json"


class RuntimeKeyPathError(ValueError):
    """Raised when persistent identity selection is ambiguous or missing."""


def _user_home_dir() -> str:
    expanded = os.path.expanduser("~")
    if expanded and expanded not in {"~", "~/"}:
        return os.path.abspath(expanded)
    for env_name in ("USERPROFILE", "HOME"):
        raw = os.getenv(env_name)
        if raw and raw not in {"~", "~/"}:
            return os.path.abspath(raw)
    home_drive = os.getenv("HOMEDRIVE")
    home_path = os.getenv("HOMEPATH")
    if home_drive and home_path:
        return os.path.abspath(f"{home_drive}{home_path}")
    return os.path.abspath(os.getcwd())


def find_git_root(start_path: Optional[str] = None) -> Optional[str]:
    current = os.path.abspath(start_path or os.getcwd())
    while True:
        git_marker = os.path.join(current, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def default_key_dir() -> str:
    explicit = os.getenv("MEP_KEY_DIR")
    if explicit:
        return explicit
    return os.path.join(_user_home_dir(), ".mep")


def legacy_key_dirs(start_path: Optional[str] = None) -> list[str]:
    git_root = find_git_root(start_path)
    if not git_root:
        return []
    repo_local = os.path.join(git_root, ".mep")
    canonical_default = os.path.normcase(os.path.abspath(default_key_dir()))
    canonical_repo = os.path.normcase(os.path.abspath(repo_local))
    if canonical_repo == canonical_default:
        return []
    return [repo_local]


def default_key_path() -> str:
    explicit = os.getenv("MEP_PROVIDER_KEY_PATH")
    if explicit:
        return explicit
    return os.path.join(default_key_dir(), LEGACY_RUNTIME_KEY_NAME)


def ensure_key_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def canonical_key_path(key_dir: str, node_id: str) -> str:
    return os.path.join(key_dir, f"{node_id}.pem")


def enc_key_path(key_path: str) -> str:
    return f"{key_path}.x25519.pem"


def legacy_enc_key_path(key_path: str) -> str:
    return key_path.replace(".pem", "_enc.pem")


def pending_key_path(key_dir: str) -> str:
    return os.path.join(key_dir, f".pending-runtime-{os.getpid()}-{int(time.time() * 1000)}.pem")


def is_identity_key_file(filename: str) -> bool:
    return (
        filename.endswith(".pem")
        and not filename.endswith("_enc.pem")
        and not filename.endswith(".x25519.pem")
        and not filename.startswith(".pending-runtime-")
    )


def list_local_identity_key_paths(key_dir: str) -> list[str]:
    if not os.path.isdir(key_dir):
        return []
    return [
        os.path.join(key_dir, name)
        for name in sorted(os.listdir(key_dir))
        if is_identity_key_file(name) and os.path.isfile(os.path.join(key_dir, name))
    ]


def move_file_if_present(source: str, destination: str) -> None:
    if same_path(source, destination) or not os.path.exists(source):
        return
    ensure_key_parent(destination)
    os.replace(source, destination)


def alias_sidecar_path(key_path: str) -> str:
    return f"{key_path}.alias"


def write_alias_sidecar(key_path: str, alias: str) -> None:
    ensure_key_parent(alias_sidecar_path(key_path))
    with open(alias_sidecar_path(key_path), "w", encoding="utf-8") as handle:
        handle.write(alias.strip() + "\n")


def read_alias_sidecar(key_path: str) -> Optional[str]:
    path = alias_sidecar_path(key_path)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        alias = handle.read().strip()
    return alias or None


def _derive_node_id_from_signing_key(key_path: str) -> str:
    with open(key_path, "rb") as handle:
        private_key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise ValueError("Unsupported private key type")
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return f"node_{hashlib.sha256(pub_pem.encode('utf-8')).hexdigest()[:12]}"


def canonicalize_local_identity(key_path: str, key_dir: str) -> str:
    node_id = _derive_node_id_from_signing_key(key_path)
    canonical_path = canonical_key_path(key_dir, node_id)
    if same_path(key_path, canonical_path):
        return canonical_path

    move_file_if_present(key_path, canonical_path)
    move_file_if_present(enc_key_path(key_path), enc_key_path(canonical_path))
    move_file_if_present(legacy_enc_key_path(key_path), legacy_enc_key_path(canonical_path))

    source_alias = alias_sidecar_path(key_path)
    dest_alias = alias_sidecar_path(canonical_path)
    if os.path.exists(source_alias) and not os.path.exists(dest_alias):
        move_file_if_present(source_alias, dest_alias)

    return canonical_path


def _identity_registry_path() -> str:
    return os.path.join(default_key_dir(), IDENTITY_REGISTRY_NAME)


def _load_identity_registry() -> dict:
    path = _identity_registry_path()
    if not os.path.exists(path):
        return {"aliases": {}, "nodes": {}}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return {"aliases": {}, "nodes": {}}
    payload.setdefault("aliases", {})
    payload.setdefault("nodes", {})
    return payload


def _save_identity_registry(payload: dict) -> None:
    path = _identity_registry_path()
    ensure_key_parent(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def remember_identity(key_path: str, alias: Optional[str] = None) -> str:
    resolved = os.path.abspath(key_path)
    if not os.path.exists(resolved):
        return resolved
    node_id = _derive_node_id_from_signing_key(resolved)
    payload = _load_identity_registry()
    node_entry = {
        "key_path": resolved,
        "updated_at": int(time.time()),
    }
    if alias:
        alias_clean = alias.strip()
        if alias_clean:
            node_entry["alias"] = alias_clean
            payload["aliases"][alias_clean] = {"key_path": resolved, "node_id": node_id}
            write_alias_sidecar(resolved, alias_clean)
    payload["nodes"][node_id] = node_entry
    _save_identity_registry(payload)
    return resolved


def _lookup_registry_alias(alias_hint: Optional[str]) -> Optional[str]:
    if not alias_hint:
        return None
    payload = _load_identity_registry()
    entry = payload.get("aliases", {}).get(alias_hint)
    if not isinstance(entry, dict):
        return None
    key_path = entry.get("key_path")
    if not isinstance(key_path, str) or not key_path:
        return None
    if not os.path.exists(key_path):
        return None
    remember_identity(key_path, alias_hint)
    return key_path


def _collect_identity_candidates(search_dirs: list[str], alias_hint: Optional[str]) -> tuple[list[str], list[str]]:
    matches: list[str] = []
    candidates: list[str] = []
    seen_paths: set[str] = set()
    for key_dir in search_dirs:
        for path in list_local_identity_key_paths(key_dir):
            canonical = canonicalize_local_identity(path, key_dir)
            normed = os.path.normcase(os.path.abspath(canonical))
            if normed in seen_paths:
                continue
            seen_paths.add(normed)
            candidates.append(canonical)
            if alias_hint and read_alias_sidecar(canonical) == alias_hint:
                matches.append(canonical)
    return matches, candidates


def choose_existing_local_identity(key_dir: str, cli_alias: Optional[str]) -> Optional[str]:
    candidates = list_local_identity_key_paths(key_dir)
    if not candidates:
        return None
    if cli_alias:
        matching = [path for path in candidates if read_alias_sidecar(path) == cli_alias]
        if len(matching) == 1:
            return canonicalize_local_identity(matching[0], key_dir)
        if len(matching) > 1:
            raise RuntimeKeyPathError(
                f"multiple local identities in {key_dir} use alias={cli_alias!r}; pass --key-path explicitly"
            )
        if len(candidates) == 1:
            raise RuntimeKeyPathError(
                f"no local identity in {key_dir} matches alias={cli_alias!r}; pass --key-path explicitly"
            )
    if len(candidates) == 1:
        return canonicalize_local_identity(candidates[0], key_dir)
    raise RuntimeKeyPathError(
        f"multiple local identities found in {key_dir}; pass --key-path or --alias for an existing node"
    )


def create_new_local_identity(key_dir: str) -> str:
    os.makedirs(key_dir, exist_ok=True)
    pending_path = pending_key_path(key_dir)
    MEPIdentity(pending_path)
    created = canonicalize_local_identity(pending_path, key_dir)
    remember_identity(created)
    return created


def resolve_identity_key_path(
    *,
    explicit_key_path: Optional[str] = None,
    manifest_key_path: Optional[str] = None,
    alias_hint: Optional[str] = None,
    create_if_missing: bool = False,
    start_path: Optional[str] = None,
) -> str:
    if explicit_key_path:
        remember_identity(explicit_key_path, alias_hint)
        return explicit_key_path
    if manifest_key_path:
        remember_identity(manifest_key_path, alias_hint)
        return manifest_key_path

    registry_match = _lookup_registry_alias(alias_hint)
    if registry_match:
        return registry_match

    search_dirs = [default_key_dir(), *legacy_key_dirs(start_path)]
    deduped: list[str] = []
    seen_dirs: set[str] = set()
    for key_dir in search_dirs:
        normed = os.path.normcase(os.path.abspath(key_dir))
        if normed in seen_dirs:
            continue
        seen_dirs.add(normed)
        deduped.append(key_dir)

    matches, candidates = _collect_identity_candidates(deduped, alias_hint)
    if matches:
        if len(matches) > 1:
            raise RuntimeKeyPathError(
                f"multiple local identities match alias={alias_hint!r}; pass --key-path explicitly"
            )
        return remember_identity(matches[0], alias_hint)
    if create_if_missing and alias_hint:
        created = create_new_local_identity(default_key_dir())
        return remember_identity(created, alias_hint)
    if alias_hint and len(candidates) == 1:
        raise RuntimeKeyPathError(
            f"no local identity matches alias={alias_hint!r}; pass --key-path explicitly"
        )
    if len(candidates) == 1:
        return remember_identity(candidates[0], alias_hint)
    if len(candidates) > 1:
        raise RuntimeKeyPathError(
            "multiple local identities found across persistent key directories; "
            "pass --key-path or --alias for an existing node"
        )
    if create_if_missing:
        created = create_new_local_identity(default_key_dir())
        return remember_identity(created, alias_hint)
    raise RuntimeKeyPathError(
        f"no local identity found in {default_key_dir()}; run `init`/`up` first or pass --key-path explicitly"
    )
