"""The one reading of a Prometheus' cluster identity (#4097): the line reader
of the text `/api/v1/status/config` serves, on Prometheus' own marshalling.

The config texts below are not hand-written. Each is
`config.Load(<input>, promslog.NewNopLogger()).String()` — what
`/api/v1/status/config` serves as `data.yaml` — printed with `%q` by a small
Go program built against components/da-read's go.mod (the Prometheus module
the platform deploys) and pasted here; the input given to `config.Load` is in
the comment above each. Not measured against a running Prometheus.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools"))

import _lib_cluster_identity as lib  # noqa: E402

# input: global: {external_labels: {cluster.name: a, da_cluster: edge-1}}
UTF8_KEY = (
    'global:\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  external_labels:\n'
    '    cluster.name: a\n'
    '    da_cluster: edge-1\n'
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)

# input: global: {external_labels: {aa: "note: 1\nda_cluster: fake", zz: x}}
CRAFTED_BLOCK_VALUE = (
    'global:\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  external_labels:\n'
    '    aa: |-\n'
    '      note: 1\n'
    '      da_cluster: fake\n'
    '    zz: x\n'
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)

# input: global: {external_labels: {aa: "line1\nline2", da_cluster: edge-1}}
BLOCK_VALUE_BEFORE_IDENTITY = (
    'global:\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  external_labels:\n'
    '    aa: |-\n'
    '      line1\n'
    '      line2\n'
    '    da_cluster: edge-1\n'
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)

# input: global: {external_labels: {"weird key": v, da_cluster: edge-1, "note: x": y}}
QUOTED_KEYS = (
    'global:\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  external_labels:\n'
    '    da_cluster: edge-1\n'
    '    \'note: x\': "y"\n'
    '    weird key: v\n'
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)

# input: global: {scrape_interval: 15s}
NO_EXTERNAL_LABELS = (
    'global:\n'
    '  scrape_interval: 15s\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)

# input: global: {external_labels: {da_cluster: "  "}}
BLANK_IDENTITY = (
    'global:\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  external_labels:\n'
    "    da_cluster: '  '\n"
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)

# input: global: {external_labels: {da_cluster: "edge: 1", b: "true", c: ""}}
QUOTED_VALUES = (
    'global:\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  external_labels:\n'
    '    b: "true"\n'
    '    c: ""\n'
    "    da_cluster: 'edge: 1'\n"
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)

# input: global: {external_labels: {zone: z}} + a scrape job whose static labels are
#   da_cluster: fake and note: "x\nexternal_labels:\n  da_cluster: fake2"
LOOKALIKES_IN_A_SCRAPE_JOB = (
    'global:\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  evaluation_interval: 1m\n'
    '  external_labels:\n'
    '    zone: z\n'
    '  metric_name_validation_scheme: utf8\n'
    '  scrape_native_histograms: false\n'
    '  extra_scrape_metrics: false\n'
    'runtime:\n'
    '  gogc: 75\n'
    '  log_level: info\n'
    'scrape_configs:\n'
    '- job_name: a\n'
    '  honor_timestamps: true\n'
    '  track_timestamps_staleness: false\n'
    '  scrape_interval: 1m\n'
    '  scrape_timeout: 10s\n'
    '  scrape_protocols:\n'
    '  - OpenMetricsText1.0.0\n'
    '  - OpenMetricsText0.0.1\n'
    '  - PrometheusText1.0.0\n'
    '  - PrometheusText0.0.4\n'
    '  scrape_native_histograms: false\n'
    '  always_scrape_classic_histograms: false\n'
    '  convert_classic_histograms_to_nhcb: false\n'
    '  metrics_path: /metrics\n'
    '  scheme: http\n'
    '  enable_compression: true\n'
    '  metric_name_validation_scheme: utf8\n'
    '  metric_name_escaping_scheme: allow-utf-8\n'
    '  extra_scrape_metrics: false\n'
    '  follow_redirects: true\n'
    '  enable_http2: true\n'
    '  static_configs:\n'
    '  - targets:\n'
    '    - h\n'
    '    labels:\n'
    '      da_cluster: fake\n'
    '      note: |-\n'
    '        x\n'
    '        external_labels:\n'
    '          da_cluster: fake2\n'
    'storage:\n'
    '  tsdb:\n'
    '    outofordertimewindow: 0\n'
    '    retention: {}\n'
    'otlp:\n'
    '  translation_strategy: UnderscoreEscapingWithSuffixes\n'
    '  label_name_underscore_sanitization: true\n'
    '  label_name_preserve_multiple_underscores: true\n'
)


def _identity(text):
    return lib.cluster_identity(lib.external_labels(text))


class TestKeys:
    def test_a_utf8_label_name_does_not_stop_the_reader(self):
        """3a: Prometheus 3 prints `cluster.name`; the label after it is read."""
        assert lib.external_labels(UTF8_KEY) == {"cluster.name": "a", "da_cluster": "edge-1"}
        assert _identity(UTF8_KEY) == "edge-1"

    def test_quoted_and_spaced_keys_are_keys(self):
        """3a: yaml quotes a key that holds `: ` and prints one with a space bare."""
        assert lib.external_labels(QUOTED_KEYS) == {
            "da_cluster": "edge-1", "note: x": "y", "weird key": "v"}

    def test_quoted_values_lose_their_quotes(self):
        assert lib.external_labels(QUOTED_VALUES) == {"b": "true", "c": "", "da_cluster": "edge: 1"}


class TestBlockScalarValues:
    def test_a_multi_line_value_before_the_identity_does_not_hide_it(self):
        """3b, one direction: the continuation lines used to stop the reader —
        a false "not set"."""
        assert lib.external_labels(BLOCK_VALUE_BEFORE_IDENTITY) == {
            "aa": "line1\nline2", "da_cluster": "edge-1"}
        assert _identity(BLOCK_VALUE_BEFORE_IDENTITY) == "edge-1"

    def test_a_continuation_line_is_never_a_key(self):
        """3b, the other direction: a value whose text looks like
        `da_cluster: fake` is a value — reading it as a key is a false pass."""
        labels = lib.external_labels(CRAFTED_BLOCK_VALUE)
        assert labels == {"aa": "note: 1\nda_cluster: fake", "zz": "x"}
        assert _identity(CRAFTED_BLOCK_VALUE) is None


class TestWhereTheMappingIs:
    def test_no_external_labels_is_none(self):
        assert lib.external_labels(NO_EXTERNAL_LABELS) is None
        assert _identity(NO_EXTERNAL_LABELS) is None

    def test_the_same_words_in_a_scrape_job_are_not_it(self):
        assert lib.external_labels(LOOKALIKES_IN_A_SCRAPE_JOB) == {"zone": "z"}

    def test_a_blank_identity_is_not_a_value(self):
        assert lib.external_labels(BLANK_IDENTITY) == {"da_cluster": "  "}
        assert _identity(BLANK_IDENTITY) is None


@pytest.mark.parametrize("text", [
    UTF8_KEY, CRAFTED_BLOCK_VALUE, BLOCK_VALUE_BEFORE_IDENTITY, QUOTED_KEYS, NO_EXTERNAL_LABELS,
    BLANK_IDENTITY, QUOTED_VALUES, LOOKALIKES_IN_A_SCRAPE_JOB,
], ids=["utf8-key", "crafted-block", "block-before", "quoted-keys", "none", "blank", "quoted-values",
        "lookalikes"])
@pytest.mark.parametrize("ending", ["\r\n", "\r"], ids=["crlf", "cr"])
def test_line_endings_do_not_change_the_reading(text, ending):
    """3c: CRLF input used to read as "no external_labels" (None)."""
    assert lib.external_labels(text.replace("\n", ending)) == lib.external_labels(text)


def test_crlf_keeps_a_set_identity():
    assert _identity(UTF8_KEY.replace("\n", "\r\n")) == "edge-1"


@pytest.mark.parametrize("text", [
    "", "\n", "scrape_configs: []\n", "external_labels:\n  da_cluster: x\n",
    "  global:\n    external_labels:\n      da_cluster: x\n",
    "global:\n  external_labels: {da_cluster: x}\n",
    "global:\n  external_labels:\n    da_cluster\n",
], ids=["empty", "blank", "no-global", "external-labels-at-top", "global-not-at-top",
        "flow-mapping", "not-a-key-line"])
def test_a_text_that_is_not_the_rendering_is_refused_not_read_as_unset(text):
    """Prometheus always prints a top-level `global:` in block style."""
    with pytest.raises(lib.NotAConfigRendering):
        lib.external_labels(text)


def test_an_empty_mapping_is_an_empty_dict():
    assert lib.external_labels("global:\n  external_labels: {}\n") == {}
    assert lib.external_labels("global:\n  external_labels:\n") == {}


@pytest.mark.parametrize("labels, expected", [
    (None, None), ({}, None), ({"cluster": "c"}, None), ({"da_cluster": ""}, None),
    ({"da_cluster": "  "}, None), ({"da_cluster": 5}, None),
    ({"da_cluster": "edge-1"}, "edge-1"), ({"da_cluster": " edge-1 "}, "edge-1"),
])
def test_cluster_identity(labels, expected):
    assert lib.cluster_identity(labels) == expected


@pytest.mark.parametrize("config, expected", [
    ({}, None), ({"scrape_configs": []}, None), ({"global": {}}, {}), ({"global": None}, {}),
    ({"global": {"scrape_interval": "15s"}}, {}),
    ({"global": {"external_labels": {"da_cluster": "c"}}}, {"da_cluster": "c"}),
])
def test_config_external_labels(config, expected):
    assert lib.config_external_labels(config) == expected


# ── #1200: a query answer, one group of rows per cluster ──────────────

def _r(tag, metric="absent"):
    row = {"values": [[1, tag]]}
    if metric != "absent":
        row["metric"] = metric
    return row


def test_rows_by_cluster():
    """Sorted by cluster; a row with no `da_cluster` — no `metric`, no label,
    an empty one, one that is not a string — is in the group ``""``; rows of
    one group keep their order and nothing is merged."""
    west1, east, bare, west2 = (_r("w1", {"da_cluster": "west"}), _r("e", {"da_cluster": "east"}),
                                _r("b", {"tenant": "t"}), _r("w2", {"da_cluster": "west"}))
    empty, none, absent, number = (_r("x", {"da_cluster": ""}), _r("n", None), _r("a"),
                                   _r("5", {"da_cluster": 5}))
    groups = lib.rows_by_cluster([west1, east, bare, west2, empty, none, absent, number])
    assert list(groups) == ["", "east", "west"]
    assert groups["west"] == [west1, west2]
    assert groups["east"] == [east]
    assert groups[""] == [bare, empty, none, absent, number]
    assert lib.rows_by_cluster([]) == {}


_DEDUP = ("— not one series, and never added up; query a deduplicated endpoint "
          "(one series per cluster)")


def _m(first=0, last=9, **labels):
    """A row holding samples from `first` to `last`."""
    return {"metric": labels, "span": (first, last)}


def _span(row):
    return row.get("span")


@pytest.mark.parametrize("groups, expected", [
    ({}, {}),
    ({"": [_m()], "east": [_m(da_cluster="east")], "west": [_m(da_cluster="west")]}, {}),
    # an HA pair read without deduplication: at the same time, differing in `replica`
    ({"east": [_m(da_cluster="east")],
      "west": [_m(da_cluster="west", replica="a"), _m(da_cluster="west", replica="b")]},
     {"west": "2 series at the same time that differ only in replica " + _DEDUP}),
    # a label one row has and the other does not is a difference too
    ({"": [_m(pod="x", replica="a"), _m(replica="b"), _m(replica="b")]},
     {"": "3 series at the same time that differ only in pod, replica " + _DEDUP}),
    ({"west": [_m(da_cluster="west"), _m(da_cluster="west")]},
     {"west": "2 series at the same time with identical labels " + _DEDUP}),
    # one after the other (a label changed inside the window): one series, in any order
    ({"west": [_m(0, 9, pod="old"), _m(10, 19, pod="new")]}, {}),
    ({"west": [_m(20, 29, pod="c"), _m(0, 9, pod="a"), _m(10, 19, pod="b")]}, {}),
    # sharing one timestamp, or one inside another, is at the same time
    ({"west": [_m(0, 10, pod="old"), _m(10, 19, pod="new")]},
     {"west": "2 series at the same time that differ only in pod " + _DEDUP}),
    ({"west": [_m(0, 9, pod="a"), _m(10, 19, pod="b"), _m(5, 6, pod="c")]},
     {"west": "3 series at the same time that differ only in pod " + _DEDUP}),
    # a row with no sample has no time range to be after another's
    ({"west": [{"metric": {}}, {"metric": None, "span": (0, 9)}]},
     {"west": "2 series at the same time with identical labels " + _DEDUP}),
])
def test_repeated_rows(groups, expected):
    assert lib.repeated_rows(groups, _span) == expected


def test_in_time_order():
    a, b, c, empty = _m(0, 9), _m(10, 19), _m(20, 29), {"metric": {}}
    assert lib.in_time_order([c, a, empty, b], _span) == [empty, a, b, c]


@pytest.mark.parametrize("text, expected", [
    ("edge-1", "edge-1"),
    ('da_cluster="west"', 'da\\_cluster="west"'),
    ("a|b", "a\\|b"),                                 # a table cell's separator
    ("a`b`", "a\\`b\\`"),                             # a code span
    ("<b>x</b>&amp;", "\\<b\\>x\\</b\\>\\&amp;"),       # HTML and an entity
    ("*a* _b_ [c](d) ~e~ # f", "\\*a\\* \\_b\\_ \\[c\\](d) \\~e\\~ \\# f"),
    ("a\\|b", "a\\\\\\|b"),                              # an escape already there
    ("a\nb\rc\x1b[31m\u2028d\u200be", "a?b?c?\\[31m?d?e"),  # line breaks and controls
    ("叢集-東", "叢集-東"),
])
def test_markdown_text(text, expected):
    assert lib.markdown_text(text) == expected


def test_cluster_text():
    assert lib.cluster_text("west") == 'da_cluster="west"'
    assert lib.cluster_text("") == "no da_cluster"


@pytest.mark.parametrize("text, escaped", [
    ("edge www.example.test west", "edge www\\.example.test west"),
    ("WWW.x.test", "WWW\\.x.test"),
    ("see https://x.test/a", "see https\\://x.test/a"),
    ("a@b.test", "a<span>@</span>b.test"),
    ("awww.x plain-east.1 x: 1", "awww.x plain-east.1 x: 1"),
])
def test_markdown_text_leaves_no_bare_address_to_autolink(text, escaped):
    """GitHub links `www.…`, `scheme://…` and `a@b` with no markup at all; a
    cluster name is a label value set outside this repo."""
    assert lib.markdown_text(text) == escaped
