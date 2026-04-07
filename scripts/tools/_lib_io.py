"""File I/O and YAML helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Optional

import yaml

from _lib_constants import ONBOARD_HINTS_FILENAME


def load_yaml_file(path: Optional[str], default: Any = None) -> Any:
    """Load a YAML file with UTF-8 encoding and safe parsing.

    Args:
        path: Filesystem path.  Returns *default* if ``None``, empty,
              or non-existent.
        default: Fallback value when the file is missing or empty.

    Returns:
        Parsed YAML data, or *default*.
    """
    if not path or not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else default


def iter_yaml_files(
    config_dir: str,
    *,
    skip_reserved: bool = True,
) -> list[tuple[str, str]]:
    """List YAML files in *config_dir*, sorted deterministically.

    Args:
        config_dir: Path to the configuration directory.
        skip_reserved: If ``True`` (default), skip files whose names
                       start with ``_`` or ``.`` (reserved / dotfiles).

    Returns:
        List of ``(filename, full_path)`` tuples, sorted by filename.
    """
    if not config_dir or not os.path.isdir(config_dir):
        return []
    result: list[tuple[str, str]] = []
    for fname in sorted(os.listdir(config_dir)):
        if not (fname.endswith(".yaml") or fname.endswith(".yml")):
            continue
        if skip_reserved and (fname.startswith("_") or fname.startswith(".")):
            continue
        fpath = os.path.join(config_dir, fname)
        if os.path.isfile(fpath):
            result.append((fname, fpath))
    return result


def load_tenant_configs(config_dir: str) -> dict[str, dict[str, Any]]:
    """Load all tenant configurations from a config directory.

    Handles both the ``{tenants: {name: {...}}}`` wrapper format
    (used in ``conf.d/``) and the flat single-tenant format.
    Files starting with ``_`` or ``.`` are skipped.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        Dict mapping ``tenant_name`` → ``config_dict``.  Empty dict
        on any error or if *config_dir* is missing.
    """
    configs: dict[str, dict[str, Any]] = {}
    for fname, fpath in iter_yaml_files(config_dir):
        raw = load_yaml_file(fpath, default={})
        if not isinstance(raw, dict):
            continue
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            for t_name, t_data in raw["tenants"].items():
                if isinstance(t_data, dict):
                    configs[t_name] = t_data
        else:
            tenant = fname.rsplit(".", 1)[0]
            configs[tenant] = raw
    return configs


def write_text_secure(path: str, content: str) -> None:
    """Write text to *path* with UTF-8 encoding and ``0o600`` permissions.

    Centralises the SAST-mandated pattern::

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(path, 0o600)

    Args:
        path: Filesystem path to write.
        content: Text content.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(path, 0o600)


def write_json_secure(
    path: str,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Write *data* as JSON to *path* with ``0o600`` permissions.

    Args:
        path: Filesystem path to write.
        data: JSON-serializable object.
        indent: JSON indentation (default 2).
        ensure_ascii: If ``False`` (default), allow non-ASCII characters.
    """
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=ensure_ascii)
    os.chmod(path, 0o600)


def write_onboard_hints(output_dir: str, hints: dict[str, Any]) -> str:
    """Write onboard hints JSON for scaffold consumption.

    Args:
        output_dir: Directory to write ``onboard-hints.json`` into.
        hints: Data dict (tenants, db_types, routing_hints, …).

    Returns:
        Absolute path to the written file.
    """
    path = os.path.join(output_dir, ONBOARD_HINTS_FILENAME)
    write_json_secure(path, hints)
    return path


def read_onboard_hints(path: Optional[str]) -> Optional[dict[str, Any]]:
    """Read onboard hints JSON.

    Returns:
        Parsed dict, or ``None`` if file is missing / unreadable.
    """
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def format_json_report(data: Any, **kwargs: Any) -> str:
    """Serialize data as pretty-printed JSON (ensure_ascii=False).

    Thin wrapper to eliminate ``json.dumps(data, indent=2, ensure_ascii=False)``
    duplication across 20+ tools.  Extra kwargs are forwarded to ``json.dumps``.
    """
    kwargs.setdefault("indent", 2)
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(data, **kwargs)


# ── Common argparse helpers ─────────────────────────────────────────
# Extracted in v2.4.0 Phase B to eliminate argparse boilerplate across 20+ tools.


def add_config_dir_arg(
    parser: argparse.ArgumentParser,
    *,
    required: bool = True,
    default: str | None = None,
    help_text: str = "Path to tenant config directory (conf.d/)",
) -> None:
    """Add ``--config-dir`` argument with standard defaults."""
    parser.add_argument(
        "--config-dir",
        required=required and default is None,
        default=default,
        help=help_text,
    )


def add_json_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "Output as JSON (for CI integration)",
) -> None:
    """Add ``--json`` boolean flag for machine-readable output."""
    parser.add_argument("--json", action="store_true", dest="json_output", help=help_text)


def add_ci_arg(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "CI mode: exit 1 on any issue",
) -> None:
    """Add ``--ci`` boolean flag for CI exit-code behaviour."""
    parser.add_argument("--ci", action="store_true", help=help_text)


def add_prometheus_arg(
    parser: argparse.ArgumentParser,
    *,
    default: str | None = None,
    help_text: str = "Prometheus URL (default: $PROMETHEUS_URL or http://localhost:9090)",
) -> None:
    """Add ``--prometheus`` argument with env-var fallback."""
    parser.add_argument(
        "--prometheus",
        default=default or os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
        help=help_text,
    )
