#!/usr/bin/env python3
"""
da-tools — Dynamic Alerting CLI Toolkit

Unified entrypoint for portable verification and migration tools.
Designed for Platform Engineers and SREs to validate integrations
without cloning the full repository.

Usage:
    da-tools <command> [options]

Commands (Prometheus API — portable):
    check-alert       Query alert firing status for a tenant
    baseline          Observe metrics and recommend thresholds
    validate          Compare old vs new recording rules (Shadow Monitoring)

Commands (File System — offline):
    migrate           Convert legacy Prometheus rules to dynamic format
    scaffold          Generate tenant configuration interactively
    offboard          Pre-check and remove a tenant configuration
    deprecate         Mark metrics as disabled across configs

Global environment variables:
    PROMETHEUS_URL    Default Prometheus endpoint (fallback for --prometheus)
"""

import os
import sys
import importlib.util


TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

# Map subcommand names to script filenames
COMMAND_MAP = {
    # Group A: Prometheus API only (portable)
    "check-alert": "check_alert.py",
    "baseline": "baseline_discovery.py",
    "validate": "validate_migration.py",
    # Group C: File system only (offline)
    "migrate": "migrate_rule.py",
    "scaffold": "scaffold_tenant.py",
    "offboard": "offboard_tenant.py",
    "deprecate": "deprecate_rule.py",
}

# Commands that accept --prometheus flag (inject env var fallback)
PROMETHEUS_COMMANDS = {"check-alert", "baseline", "validate"}


def print_usage():
    """Print help message."""
    print(__doc__.strip())
    print()
    print("Examples:")
    print("  da-tools check-alert MariaDBHighConnections db-a --prometheus http://prometheus:9090")
    print("  da-tools baseline --tenant db-a --prometheus http://prometheus:9090")
    print("  da-tools validate --mapping mapping.csv --prometheus http://prometheus:9090")
    print("  da-tools migrate legacy-rules.yml --dry-run --triage")
    print("  da-tools scaffold --tenant db-c --db mariadb,redis --non-interactive")
    print()
    print("Environment:")
    print("  PROMETHEUS_URL   Default Prometheus endpoint (used when --prometheus is omitted)")
    sys.exit(0)


def inject_prometheus_env(args):
    """If --prometheus is not in args, inject PROMETHEUS_URL env var as default."""
    if "--prometheus" not in args:
        prom_url = os.environ.get("PROMETHEUS_URL")
        if prom_url:
            args.extend(["--prometheus", prom_url])
    return args


def run_tool(script_name, args):
    """Load and execute a tool script by rewriting sys.argv."""
    script_path = os.path.join(TOOLS_DIR, script_name)

    if not os.path.isfile(script_path):
        print(f"Error: Tool script not found: {script_path}", file=sys.stderr)
        sys.exit(1)

    # Rewrite sys.argv so argparse in each tool sees correct arguments
    sys.argv = [script_name] + args

    # Load and execute the script as __main__
    spec = importlib.util.spec_from_file_location("__main__", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_usage()

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "--version":
        version_file = os.path.join(TOOLS_DIR, "VERSION")
        if os.path.isfile(version_file):
            with open(version_file) as f:
                print(f"da-tools {f.read().strip()}")
        else:
            print("da-tools (dev)")
        sys.exit(0)

    if command not in COMMAND_MAP:
        print(f"Error: Unknown command '{command}'", file=sys.stderr)
        print(f"Available commands: {', '.join(sorted(COMMAND_MAP.keys()))}", file=sys.stderr)
        print("Run 'da-tools --help' for usage.", file=sys.stderr)
        sys.exit(1)

    # Inject PROMETHEUS_URL for applicable commands
    if command in PROMETHEUS_COMMANDS:
        args = inject_prometheus_env(args)

    run_tool(COMMAND_MAP[command], args)


if __name__ == "__main__":
    main()
