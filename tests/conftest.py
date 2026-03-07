"""Shared test fixtures and helpers for da-tools test suite."""
import os
import stat


def write_yaml(tmpdir, filename, content):
    """Write a YAML file into tmpdir with secure permissions.

    Returns the absolute path to the written file.
    """
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path
