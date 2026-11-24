"""The validation report `validate` writes and `shadow-verify` reads (#3662).

⛔ The one definition of ``validation-report.csv``: its columns and its
timestamp format. ``validate_migration.write_csv_report`` writes the header
from :data:`REPORT_COLUMNS` and each row's ``Timestamp`` with
:data:`TIMESTAMP_FORMAT`; ``shadow-verify --report-csv`` declares the same
tuple as the header the file must have (``_lib_input.Document("csv", ...,
columns=REPORT_COLUMNS)``) and reads ``Timestamp`` with
:func:`parse_timestamp` (every row's form is judged at reading:
:data:`REPORT_FORMATS`). A file whose header is anything else — another
tool's CSV that happens to hold a ``Status`` column, a hand-made subset — was
read as a report with no mismatch (rc 0); now it is refused, rc 2.

A row is one (tenant, cluster) group (ADR-006, #1200). The header has no
column for the cluster, and a file without clusters is what it always was:
``Tenant`` holds the tenant, and for a row whose series carried ``da_cluster``
it holds :func:`tenant_cell` — ``<tenant> (da_cluster=<cluster>)`` — which
:func:`split_tenant_cell` reads back.

Standard library and `_lib_cluster_identity` (itself standard library only).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from _lib_cluster_identity import CLUSTER_IDENTITY_LABEL

#: The header of validation-report.csv, in order.
REPORT_COLUMNS = (
    "Label", "Tenant", "Old Query", "New Query",
    "Old Value", "New Value", "Delta", "Status",
    "Timestamp",
)

#: How a row's ``Timestamp`` is written (UTC is not stated in the value:
#: the reader takes it as UTC, as it always has).
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: The columns whose every value has a set form, as
#: ``_lib_input.Document(formats=...)`` declares it.
REPORT_FORMATS = (("Timestamp", TIMESTAMP_FORMAT),)


def parse_timestamp(value: str) -> datetime:
    """The ``Timestamp`` *value* of a row as an aware UTC datetime. Raises
    ``ValueError`` for a value not written in :data:`TIMESTAMP_FORMAT`."""
    return datetime.strptime(value.strip(), TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


_CLUSTER_SUFFIX = re.compile(
    r"^(?P<tenant>.*) \(" + re.escape(CLUSTER_IDENTITY_LABEL) + r"=(?P<cluster>.+)\)$",
    re.DOTALL)


def tenant_cell(tenant: str, cluster: "str | None" = None) -> str:
    """What the ``Tenant`` column holds for the group (*tenant*, *cluster*):
    the tenant alone when the row has no cluster."""
    if not cluster:
        return tenant
    return f"{tenant} ({CLUSTER_IDENTITY_LABEL}={cluster})"


def split_tenant_cell(cell: str) -> "tuple[str, str | None]":
    """``(tenant, cluster)`` of a ``Tenant`` cell; cluster None for a row
    written without one."""
    m = _CLUSTER_SUFFIX.match(cell)
    return (m["tenant"], m["cluster"]) if m else (cell, None)
