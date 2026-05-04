"""Tests for check_routing_profiles.py (ADR-007 lint tool)."""
from __future__ import annotations

import os
import sys
import textwrap
import tempfile

import pytest
import yaml

# ---------------------------------------------------------------------------
# sys.path setup (conftest.py handles this, but be explicit for clarity)
# ---------------------------------------------------------------------------
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, 'scripts', 'tools', 'lint'))
sys.path.insert(0, os.path.join(_REPO, 'scripts', 'tools'))

from check_routing_profiles import _collect_data, validate  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================

def _write(tmpdir: str, filename: str, data: dict) -> str:
    """Write a YAML dict to tmpdir and return path."""
    path = os.path.join(tmpdir, filename)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    return path


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def config_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def full_setup(config_dir):
    """Set up config_dir with profiles, policies, and tenant files."""
    # Routing profiles
    _write(config_dir, '_routing_profiles.yaml', {
        'routing_profiles': {
            'team-sre-apac': {
                'receiver': {'type': 'slack'},
                'group_wait': '30s',
            },
            'team-dba-global': {
                'receiver': {'type': 'webhook'},
                'group_wait': '1m',
            },
        }
    })

    # Domain policies
    _write(config_dir, '_domain_policy.yaml', {
        'domain_policies': {
            'finance': {
                'tenants': ['db-a'],
                'constraints': {
                    'forbidden_receiver_types': ['webhook'],
                    'max_repeat_interval': '4h',
                },
            }
        }
    })

    # Tenant files
    _write(config_dir, 'db-a.yaml', {
        'tenants': {
            'db-a': {
                'cpu_usage': '80',
                '_routing_profile': 'team-sre-apac',
            }
        }
    })
    _write(config_dir, 'db-b.yaml', {
        'tenants': {
            'db-b': {
                'cpu_usage': '90',
                '_routing_profile': 'team-dba-global',
            }
        }
    })

    return config_dir


# ===========================================================================
# _collect_data tests
# ===========================================================================

class TestCollectData:
    def test_collects_profiles(self, full_setup):
        data = _collect_data(full_setup)
        assert 'team-sre-apac' in data['profiles']
        assert 'team-dba-global' in data['profiles']

    def test_collects_policies(self, full_setup):
        data = _collect_data(full_setup)
        assert 'finance' in data['policies']

    def test_collects_tenant_ids(self, full_setup):
        data = _collect_data(full_setup)
        assert data['tenant_ids'] == {'db-a', 'db-b'}

    def test_collects_profile_refs(self, full_setup):
        data = _collect_data(full_setup)
        assert data['profile_refs'] == {
            'db-a': 'team-sre-apac',
            'db-b': 'team-dba-global',
        }

    def test_empty_dir(self, config_dir):
        data = _collect_data(config_dir)
        assert data['profiles'] == {}
        assert data['policies'] == {}
        assert data['tenant_ids'] == set()
        assert data['profile_refs'] == {}

    def test_profiles_only(self, config_dir):
        _write(config_dir, '_routing_profiles.yaml', {
            'routing_profiles': {'p1': {'receiver': {'type': 'slack'}}}
        })
        data = _collect_data(config_dir)
        assert 'p1' in data['profiles']
        assert data['profile_refs'] == {}

    def test_yml_extension(self, config_dir):
        _write(config_dir, '_routing_profiles.yml', {
            'routing_profiles': {'p1': {'receiver': {'type': 'slack'}}}
        })
        data = _collect_data(config_dir)
        assert 'p1' in data['profiles']

    def test_skips_hidden_files(self, config_dir):
        _write(config_dir, '.hidden.yaml', {
            'tenants': {'secret': {'cpu': '99'}}
        })
        data = _collect_data(config_dir)
        assert 'secret' not in data['tenant_ids']

    def test_non_dict_data_skipped(self, config_dir):
        path = os.path.join(config_dir, 'bad.yaml')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('- just a list\n')
        data = _collect_data(config_dir)
        assert data['tenant_ids'] == set()

    def test_tenant_without_profile_ref(self, config_dir):
        _write(config_dir, 'db-x.yaml', {
            'tenants': {'db-x': {'cpu_usage': '80'}}
        })
        data = _collect_data(config_dir)
        assert 'db-x' in data['tenant_ids']
        assert 'db-x' not in data['profile_refs']


# ===========================================================================
# validate tests
# ===========================================================================

class TestValidate:
    def test_all_valid(self, full_setup):
        data = _collect_data(full_setup)
        messages = validate(data)
        # No errors or warns; may have INFO for orphan profiles
        errors = [m for m in messages if m.startswith('ERROR') or m.startswith('WARN')]
        assert errors == []

    def test_unknown_profile_ref(self, config_dir):
        _write(config_dir, '_routing_profiles.yaml', {
            'routing_profiles': {'p1': {'receiver': {'type': 'slack'}}}
        })
        _write(config_dir, 'db-a.yaml', {
            'tenants': {'db-a': {'_routing_profile': 'nonexistent'}}
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        warns = [m for m in messages if 'WARN' in m]
        assert any("nonexistent" in m for m in warns)

    def test_unknown_profile_ref_strict(self, config_dir):
        _write(config_dir, '_routing_profiles.yaml', {
            'routing_profiles': {'p1': {'receiver': {'type': 'slack'}}}
        })
        _write(config_dir, 'db-a.yaml', {
            'tenants': {'db-a': {'_routing_profile': 'nonexistent'}}
        })
        data = _collect_data(config_dir)
        messages = validate(data, strict=True)
        errors = [m for m in messages if m.startswith('ERROR')]
        assert any("nonexistent" in m for m in errors)

    def test_policy_unknown_tenant(self, config_dir):
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'finance': {
                    'tenants': ['ghost-tenant'],
                    'constraints': {},
                }
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        assert any("ghost-tenant" in m for m in messages)

    def test_policy_unknown_tenant_strict(self, config_dir):
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'finance': {
                    'tenants': ['ghost-tenant'],
                    'constraints': {},
                }
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data, strict=True)
        errors = [m for m in messages if m.startswith('ERROR')]
        assert any("ghost-tenant" in m for m in errors)

    def test_malformed_policy_not_dict(self, config_dir):
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'bad_policy': 'not a dict'
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        assert any("not a dict" in m for m in messages)

    def test_policy_tenants_not_list(self, config_dir):
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'bad': {'tenants': 'should-be-list', 'constraints': {}}
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        assert any("must be a list" in m for m in messages)

    def test_unknown_constraint_key(self, config_dir):
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'test': {
                    'tenants': [],
                    'constraints': {'unknown_key': 'value'},
                }
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        assert any("unknown constraint" in m for m in messages)

    def test_valid_constraint_keys_accepted(self, config_dir):
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'test': {
                    'tenants': [],
                    'constraints': {
                        'allowed_receiver_types': ['slack'],
                        'forbidden_receiver_types': ['webhook'],
                        'enforce_group_by': ['tenant'],
                        'max_repeat_interval': '4h',
                        'min_group_wait': '10s',
                        'require_critical_escalation': True,
                    },
                }
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        constraint_warns = [m for m in messages if 'unknown constraint' in m]
        assert constraint_warns == []

    def test_orphan_profile_info(self, config_dir):
        _write(config_dir, '_routing_profiles.yaml', {
            'routing_profiles': {
                'orphan-profile': {'receiver': {'type': 'slack'}},
                'used-profile': {'receiver': {'type': 'webhook'}},
            }
        })
        _write(config_dir, 'db-a.yaml', {
            'tenants': {'db-a': {'_routing_profile': 'used-profile'}}
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        info = [m for m in messages if m.startswith('INFO')]
        assert any("orphan-profile" in m for m in info)
        assert not any("used-profile" in m for m in info)

    def test_no_orphan_when_all_referenced(self, config_dir):
        _write(config_dir, '_routing_profiles.yaml', {
            'routing_profiles': {
                'p1': {'receiver': {'type': 'slack'}},
            }
        })
        _write(config_dir, 'db-a.yaml', {
            'tenants': {'db-a': {'_routing_profile': 'p1'}}
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        info = [m for m in messages if m.startswith('INFO')]
        assert info == []

    def test_empty_data(self):
        data = {
            'profiles': {},
            'policies': {},
            'tenant_ids': set(),
            'profile_refs': {},
        }
        messages = validate(data)
        assert messages == []

    def test_constraints_not_dict(self, config_dir):
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'bad': {
                    'tenants': [],
                    'constraints': 'not-a-dict',
                }
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        assert any("must be a dict" in m for m in messages)

    def test_multiple_policies_validated(self, config_dir):
        _write(config_dir, 'db-a.yaml', {
            'tenants': {'db-a': {'cpu': '80'}}
        })
        _write(config_dir, '_domain_policy.yaml', {
            'domain_policies': {
                'p1': {
                    'tenants': ['db-a'],
                    'constraints': {'bad_key': 1},
                },
                'p2': {
                    'tenants': ['missing-tenant'],
                    'constraints': {'another_bad': 2},
                },
            }
        })
        data = _collect_data(config_dir)
        messages = validate(data)
        # Should have both unknown constraint warns and missing tenant warn
        assert any('bad_key' in m for m in messages)
        assert any('another_bad' in m for m in messages)
        assert any('missing-tenant' in m for m in messages)


# ===========================================================================
# CLI integration tests
# ===========================================================================

class TestCLI:
    def test_valid_config(self, full_setup):
        """Valid config → exit 0."""
        import subprocess
        script = os.path.join(
            _REPO, 'scripts', 'tools', 'lint', 'check_routing_profiles.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, '--config-dir', full_setup],
            capture_output=True, text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        assert 'OK' in result.stdout

    def test_strict_with_errors(self, config_dir):
        """Strict mode with unknown profile ref → exit 1."""
        import subprocess
        _write(config_dir, '_routing_profiles.yaml', {
            'routing_profiles': {'p1': {'receiver': {'type': 'slack'}}}
        })
        _write(config_dir, 'db-a.yaml', {
            'tenants': {'db-a': {'_routing_profile': 'bad-ref'}}
        })
        script = os.path.join(
            _REPO, 'scripts', 'tools', 'lint', 'check_routing_profiles.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, '--config-dir', config_dir, '--strict'],
            capture_output=True, text=True, encoding='utf-8'
        )
        assert result.returncode == 1
        assert 'bad-ref' in result.stderr

    def test_missing_config_dir(self):
        """Non-existent config-dir → exit 1."""
        import subprocess
        script = os.path.join(
            _REPO, 'scripts', 'tools', 'lint', 'check_routing_profiles.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, '--config-dir', '/nonexistent/path'],
            capture_output=True, text=True, encoding='utf-8'
        )
        assert result.returncode == 1

    def test_warn_only_no_strict(self, config_dir):
        """Warnings without --strict → exit 0."""
        import subprocess
        _write(config_dir, '_routing_profiles.yaml', {
            'routing_profiles': {'p1': {'receiver': {'type': 'slack'}}}
        })
        _write(config_dir, 'db-a.yaml', {
            'tenants': {'db-a': {'_routing_profile': 'unknown-ref'}}
        })
        script = os.path.join(
            _REPO, 'scripts', 'tools', 'lint', 'check_routing_profiles.py')
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, '--config-dir', config_dir],
            capture_output=True, text=True, encoding='utf-8'
        )
        # WARN only (not strict) → exit 0
        assert result.returncode == 0
        assert 'unknown-ref' in result.stderr
