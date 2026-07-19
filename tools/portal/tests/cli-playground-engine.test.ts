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
