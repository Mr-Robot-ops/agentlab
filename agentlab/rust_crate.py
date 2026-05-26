from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class RustCrateLayout:
    root: str
    cargo_toml: str
    package_name: str | None
    import_name: str | None
    has_lib_rs: bool
    has_lib_section: bool
    has_main_rs: bool
    source_files: tuple[str, ...]

    @property
    def has_library(self) -> bool:
        return self.has_lib_rs or self.has_lib_section

    @property
    def is_binary_only(self) -> bool:
        return self.has_main_rs and not self.has_library


def rust_crate_layout(root: str, files: Iterable[str], read_file: Callable[[str], str]) -> RustCrateLayout:
    normalized_root = _normalize_root(root)
    normalized_files = {path.replace("\\", "/") for path in files}
    cargo_toml = _join_root(normalized_root, "Cargo.toml")
    payload = _read_cargo_toml(cargo_toml, read_file)
    package_name = _package_name(payload)
    lib_section = payload.get("lib") if isinstance(payload, dict) else None
    has_lib_section = isinstance(lib_section, dict)
    lib_path = lib_section.get("path") if isinstance(lib_section, dict) else None
    explicit_lib_path = _join_root(normalized_root, str(lib_path)) if lib_path else None
    default_lib_path = _join_root(normalized_root, "src/lib.rs")
    has_lib_rs = default_lib_path in normalized_files or (explicit_lib_path is not None and explicit_lib_path in normalized_files)
    has_main_rs = _join_root(normalized_root, "src/main.rs") in normalized_files
    source_prefix = "src/" if normalized_root == "." else f"{normalized_root}/src/"
    source_files = tuple(sorted(path for path in normalized_files if path.startswith(source_prefix) and path.endswith(".rs")))
    return RustCrateLayout(
        root=normalized_root,
        cargo_toml=cargo_toml,
        package_name=package_name,
        import_name=package_name.replace("-", "_") if package_name else None,
        has_lib_rs=has_lib_rs,
        has_lib_section=has_lib_section,
        has_main_rs=has_main_rs,
        source_files=source_files,
    )


def rust_root_for_path(path: str, files: Iterable[str]) -> str | None:
    normalized = path.replace("\\", "/")
    normalized_files = {item.replace("\\", "/") for item in files}
    parts = normalized.split("/")
    for index in range(len(parts), -1, -1):
        root = "/".join(parts[:index])
        cargo_toml = _join_root(root, "Cargo.toml")
        if cargo_toml in normalized_files:
            return _normalize_root(root)
    if normalized.startswith("rust-backend/") and "rust-backend/Cargo.toml" in normalized_files:
        return "rust-backend"
    if "Cargo.toml" in normalized_files:
        return "."
    return None


def is_rust_integration_test(path: str, root: str) -> bool:
    normalized = path.replace("\\", "/")
    normalized_root = _normalize_root(root)
    prefix = "tests/" if normalized_root == "." else f"{normalized_root}/tests/"
    return normalized.startswith(prefix) and normalized.endswith(".rs")


def _read_cargo_toml(path: str, read_file: Callable[[str], str]) -> dict[str, object]:
    try:
        payload = tomllib.loads(read_file(path))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _package_name(payload: dict[str, object]) -> str | None:
    package = payload.get("package")
    if not isinstance(package, dict):
        return None
    name = package.get("name")
    return str(name) if name else None


def _normalize_root(root: str) -> str:
    normalized = root.replace("\\", "/").strip("/")
    return normalized if normalized else "."


def _join_root(root: str, path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    normalized_root = _normalize_root(root)
    return normalized if normalized_root == "." else f"{normalized_root}/{normalized}"
