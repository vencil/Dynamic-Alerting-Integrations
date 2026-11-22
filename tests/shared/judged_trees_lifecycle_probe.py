"""Run by test_judged_trees_env_isolation.py in a pytest of its own, in this
order, in one process (not collected by name: it is no `test_*.py`): the
first test leaves `DA_JUDGED_TREES` changed, the second sees what the
conftest guard left of it."""
import os

import _lib_parse_cache as pc

LEFT_BEHIND = "left-behind-by-the-test-before"


def test_1_leaves_the_variable_changed():
    os.environ[pc.JUDGED_TREES_ENV] = LEFT_BEHIND


def test_2_starts_without_it():
    assert os.environ.get(pc.JUDGED_TREES_ENV) != LEFT_BEHIND
