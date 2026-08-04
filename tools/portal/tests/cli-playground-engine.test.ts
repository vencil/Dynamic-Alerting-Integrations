/**
 * Unit tests for cli-playground's engine (extracted from cli-playground.jsx
 * into cli-playground/engine.js this PR).
 *
 * buildCommand was an inline component closure over 6 state vars and the
 * 688-LOC tool had ZERO tests; the command string it builds is what the user
 * copy-pastes, so a regression = wrong command. buildCommand is now a pure
 * function taking the selection state; these tests pin its output with real
 * golden strings (docker/non-docker/checkbox/network branches) plus the
 * initCommandState + readHashCmd helpers. Behaviour is preserved verbatim.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { COMMANDS, NETWORK_MODES } from '../src/interactive/tools/cli-playground/commands.js';
import {
  initCommandState,
  readHashCmd,
  buildCommand,
} from '../src/interactive/tools/cli-playground/engine.js';

describe('initCommandState', () => {
  it('seeds empty strings for a real command’s args and flags', () => {
    expect(initCommandState('check-alert')).toEqual({
      args: { alert_name: '', tenant: '' },
      flags: { '--prometheus': '' },
    });
  });
});

describe('diagnose emits a command that actually runs', () => {
  // `diagnose.py --help`: [--prometheus] [--config-dir] [--show-inheritance]
  // [--json] tenant  — `tenant` is a POSITIONAL. Modelling it as `--tenant`
  // made the builder emit `da-tools diagnose --tenant db-a`, which exits with
  // "unrecognized arguments: --tenant". Handing over a paste-able line is this
  // tool's entire job, so the shape is pinned here rather than only in the
  // preview string.
  it('puts the tenant positionally, never as a --tenant flag', () => {
    const cmd = COMMANDS['diagnose'];
    expect(cmd.args.map(a => a.name)).toEqual(['tenant']);
    expect(cmd.flags.map(f => f.name)).not.toContain('--tenant');

    const built = buildCommand({
      isDocker: false,
      network: {},
      selectedCommand: 'diagnose',
      command: cmd,
      args: { tenant: 'db-a' },
      flags: { '--prometheus': 'http://localhost:9090' },
    });
    expect(built).toBe('da-tools diagnose db-a --prometheus http://localhost:9090');
    expect(built).not.toContain('--tenant');
  });

  it('renders --show-inheritance as a valueless checkbox flag', () => {
    // Without `type: 'checkbox'` engine.js takes the value branch and emits
    // `--show-inheritance <value>`, which argparse rejects.
    const flag = COMMANDS['diagnose'].flags.find(f => f.name === '--show-inheritance');
    expect(flag?.type).toBe('checkbox');

    const built = buildCommand({
      isDocker: false,
      network: {},
      selectedCommand: 'diagnose',
      command: COMMANDS['diagnose'],
      args: { tenant: 'db-a' },
      flags: { '--config-dir': 'conf.d/', '--show-inheritance': true },
    });
    expect(built).toBe(
      'da-tools diagnose db-a --config-dir conf.d/ --show-inheritance');
  });

  it('advertises exactly the flags the CLI has — no more, no fewer', () => {
    // `--namespace` was listed for years; diagnose.py has never accepted it.
    // Exact-set rather than one-way containment: a one-way check passes while
    // a real flag silently goes unadvertised, which is how `--json` was
    // missing until CodeRabbit flagged it on #1336.
    // Ground truth = `diagnose.py --help`:
    //   [--prometheus] [--config-dir] [--show-inheritance] [--json] tenant
    const real = ['--prometheus', '--config-dir', '--show-inheritance', '--json'];
    expect(COMMANDS['diagnose'].flags.map(f => f.name).sort())
      .toEqual([...real].sort());
  });

  it('emits --json as a valueless flag', () => {
    const built = buildCommand({
      isDocker: false,
      network: {},
      selectedCommand: 'diagnose',
      command: COMMANDS['diagnose'],
      args: { tenant: 'db-a' },
      flags: { '--json': true },
    });
    expect(built).toBe('da-tools diagnose db-a --json');
  });
});

describe('readHashCmd', () => {
  afterEach(() => {
    window.location.hash = '';
  });
  it('defaults to check-alert with no hash', () => {
    window.location.hash = '';
    expect(readHashCmd()).toBe('check-alert');
  });
  it('reads a valid ?cmd= from the URL hash', () => {
    window.location.hash = '#cmd=diagnose';
    expect(readHashCmd()).toBe('diagnose');
  });
  it('falls back to check-alert for an unknown cmd', () => {
    window.location.hash = '#cmd=__nope__';
    expect(readHashCmd()).toBe('check-alert');
  });
});

describe('buildCommand', () => {
  const checkAlert = {
    isDocker: false,
    network: NETWORK_MODES.linux,
    selectedCommand: 'check-alert',
    command: COMMANDS['check-alert'],
    args: { alert_name: 'HighMemoryUsage', tenant: 'acme' },
    flags: { '--prometheus': 'http://prom:9090' },
  };

  it('non-docker mode → da-tools prefix with positional args and --prometheus flag', () => {
    expect(buildCommand(checkAlert)).toBe(
      'da-tools check-alert HighMemoryUsage acme --prometheus http://prom:9090',
    );
  });

  it('docker mode → docker run wrapper, network flag, PROMETHEUS_URL env, and --prometheus SKIPPED', () => {
    expect(buildCommand({ ...checkAlert, isDocker: true })).toBe(
      'docker run --rm --network=host -e PROMETHEUS_URL=http://localhost:9090 ' +
        'ghcr.io/vencil/da-tools:v2.7.0 check-alert HighMemoryUsage acme',
    );
  });

  it('docker mode with an empty network string omits the --network prefix', () => {
    expect(buildCommand({ ...checkAlert, isDocker: true, network: NETWORK_MODES.k8s })).toBe(
      'docker run --rm -e PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090 ' +
        'ghcr.io/vencil/da-tools:v2.7.0 check-alert HighMemoryUsage acme',
    );
  });

  it('checkbox flags append the bare flag when true and are omitted when false', () => {
    const synthetic = {
      isDocker: false,
      network: NETWORK_MODES.linux,
      selectedCommand: 'demo',
      command: { args: [{ name: 'target' }], flags: [{ name: '--verbose', type: 'checkbox' }] },
      args: { target: 't1' },
      flags: { '--verbose': true },
    };
    expect(buildCommand(synthetic)).toBe('da-tools demo t1 --verbose');
    expect(buildCommand({ ...synthetic, flags: { '--verbose': false } })).toBe('da-tools demo t1');
  });

  it('omits args/flags whose value is empty', () => {
    expect(
      buildCommand({ ...checkAlert, args: { alert_name: 'OnlyThis', tenant: '' }, flags: { '--prometheus': '' } }),
    ).toBe('da-tools check-alert OnlyThis');
  });
});
