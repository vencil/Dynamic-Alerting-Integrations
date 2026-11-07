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

Standard library only.
"""
from __future__ import annotations

from datetime import datetime, timezone

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
