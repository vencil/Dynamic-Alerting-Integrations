---
title: "da-tools CLI Playground - command-string engine"
purpose: |
  Pure helpers extracted from cli-playground.jsx: initial args/flags state
  for a command, the URL-hash -> command resolver, and the command-string
  builder. buildCommand was an inline component closure; it is now a pure
  function taking the selection state, so all three are unit-testable
  directly. Behaviour is preserved verbatim.
---

import { COMMANDS } from './commands.js';

// Build initial state for a command's args/flags
function initCommandState(cmdKey) {
  const cmd = COMMANDS[cmdKey];
  const a = {};
  const f = {};
  cmd.args.forEach(arg => { a[arg.name] = ''; });
  cmd.flags.forEach(flag => { f[flag.name] = flag.type === 'checkbox' ? false : ''; });
  return { args: a, flags: f };
}

function readHashCmd() {
  try {
    const p = new URLSearchParams(window.location.hash.slice(1));
    const cmd = p.get('cmd');
    return (cmd && COMMANDS[cmd]) ? cmd : 'check-alert';
  } catch(e) { return 'check-alert'; }
}

/* buildCommand - assemble the copy-paste da-tools command string from the
 * current selection. Extracted from the CLIPlayground component (was an
 * inline closure); now a pure function taking the pieces of state it needs,
 * so it can be unit-tested directly. Behaviour is preserved verbatim. */
function buildCommand({ isDocker, network, selectedCommand, command, args, flags }) {
  let cmd = '';

  if (isDocker) {
    cmd = 'docker run --rm ';
    if (network.network) cmd += network.network + ' ';
    cmd += `-e PROMETHEUS_URL=${network.prometheus} `;
    cmd += 'ghcr.io/vencil/da-tools:v2.7.0 ';
  } else {
    cmd = 'da-tools ';
  }

  cmd += selectedCommand;

  // Add positional arguments
  command.args.forEach(arg => {
    const value = args[arg.name];
    if (value) {
      cmd += ` ${value}`;
    }
  });

  // Add flags
  command.flags.forEach(flag => {
    const value = flags[flag.name];
    if (flag.type === 'checkbox') {
      if (value) cmd += ` ${flag.name}`;
    } else if (value) {
      // Skip Prometheus URL for docker mode (passed via env var)
      if (isDocker && flag.name === '--prometheus') return;
      cmd += ` ${flag.name} ${value}`;
    }
  });

  return cmd;
}

export { initCommandState, readHashCmd, buildCommand };
