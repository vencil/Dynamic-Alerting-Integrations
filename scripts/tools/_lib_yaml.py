"""Minimal CRD YAML serialization helpers for operator tooling.

Extracted in v2.10.0 (da-tools ROI round 5, Wave 2) from the byte-identical
``write_yaml_crd`` + ``_dict_to_yaml`` pair that was duplicated verbatim in
``ops/operator_generate.py`` and ``ops/migrate_to_operator.py``.

``write_yaml_crd`` prefers PyYAML when available and falls back to the
dependency-free ``_dict_to_yaml`` emitter otherwise. The module-level
``yaml`` is imported *optionally* — mirroring the original two call sites —
so the fallback branch stays reachable (and monkeypatchable in tests) even
though the shipped Docker image always has PyYAML available transitively via
``_lib_io``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - shipped image always has PyYAML
    yaml = None

from _lib_io import write_text_secure


def write_yaml_crd(
    output_path: Path,
    crd: dict,
    gitops: bool = False,
) -> None:
    """Write CRD to YAML file.

    Args:
        output_path: Output file path
        crd: CRD dict to serialize
        gitops: If True, use sorted keys and exclude timestamps
    """
    if yaml:
        # Use yaml module if available
        yaml_str = yaml.dump(
            crd,
            default_flow_style=False,
            sort_keys=gitops,
            allow_unicode=True,
        )
    else:
        # Fallback: minimal YAML serialization
        yaml_str = _dict_to_yaml(crd)

    write_text_secure(str(output_path), yaml_str)


def _dict_to_yaml(obj: Any, indent: int = 0) -> str:
    """Minimal YAML serialization (fallback when yaml module unavailable)."""
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            val_str = _dict_to_yaml(v, indent + 2)
            if "\n" in val_str:
                lines.append(f"{' ' * indent}{k}:\n{val_str}")
            else:
                lines.append(f"{' ' * indent}{k}: {val_str}")
        return "\n".join(lines)
    elif isinstance(obj, list):
        lines = []
        for item in obj:
            item_str = _dict_to_yaml(item, indent + 2)
            if "\n" in item_str:
                lines.append(f"{' ' * indent}-\n{item_str}")
            else:
                lines.append(f"{' ' * indent}- {item_str}")
        return "\n".join(lines)
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif isinstance(obj, str):
        # Quote strings that need it
        if any(c in obj for c in ":[]{},'\""):
            return f'"{obj}"'
        return obj
    else:
        return str(obj)
