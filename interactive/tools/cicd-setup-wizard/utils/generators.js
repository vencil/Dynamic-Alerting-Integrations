---
title: "CI/CD Setup Wizard — command + config generators"
purpose: |
  Pure functions that build the artifacts the wizard ships back to
  the user: `da-tools init` command, equivalent docker run command,
  generated repo file tree, and a sample GitHub Actions YAML.

  Pre-PR-portal-10 these were inline at the top of cicd-setup-
  wizard.jsx. Splitting drops ~115 LOC from the orchestrator and
  matches the operator-setup-wizard pattern from PR-portal-4.

  Public API:
    window.__cicdGenerateInitCommand(config)            build da-tools CLI
    window.__cicdGenerateDockerCommand(config)          docker wrapper
    window.__cicdGenerateFileTree(config)               ASCII repo tree
    window.__cicdGenerateGitHubActionsPreview(config)   sample workflow YAML

  Closure deps: none. Pure functions; receive config as arg.
---

function cicdGenerateInitCommand(config) {
  const parts = ['da-tools init'];
  if (config.ci) parts.push(`--ci ${config.ci}`);
  if (config.deploy) parts.push(`--deploy ${config.deploy}`);
  if (config.tenants.length > 0) parts.push(`--tenants ${config.tenants.join(',')}`);
  if (config.packs.length > 0) parts.push(`--rule-packs ${config.packs.join(',')}`);
  parts.push('--non-interactive');
  return parts.join(' \\\n  ');
}

function cicdGenerateDockerCommand(config) {
  const init = cicdGenerateInitCommand(config);
  return `docker run --rm -it \\\n  -v $(pwd):/workspace -w /workspace \\\n  ghcr.io/vencil/da-tools:latest \\\n  ${init.replace('da-tools ', '')}`;
}

function cicdGenerateFileTree(config) {
  const lines = ['your-repo/'];
  lines.push('├── conf.d/');
  lines.push('│   ├── _defaults.yaml');
  for (const tenant of config.tenants) {
    lines.push(`│   └── ${tenant}.yaml`);
  }
  if (config.ci === 'github' || config.ci === 'both') {
    lines.push('├── .github/workflows/');
    lines.push('│   └── dynamic-alerting.yaml');
  }
  if (config.ci === 'gitlab' || config.ci === 'both') {
    lines.push('├── .gitlab-ci.d/');
    lines.push('│   └── dynamic-alerting.yml');
  }
  if (config.deploy === 'kustomize' || config.deploy === 'argocd') {
    lines.push('├── kustomize/');
    lines.push('│   ├── base/');
    lines.push('│   │   └── kustomization.yaml');
    lines.push('│   └── overlays/');
    lines.push('│       ├── dev/');
    lines.push('│       └── prod/');
  }
  if (config.deploy === 'argocd') {
    lines.push('├── argocd/');
    lines.push('│   └── dynamic-alerting.yaml');
  }
  lines.push('├── .pre-commit-config.da.yaml');
  lines.push('└── .da-init.yaml');
  return lines.join('\n');
}

function cicdGenerateGitHubActionsPreview(config) {
  return `name: Dynamic Alerting CI/CD
on:
  pull_request:
    paths: ['conf.d/**']
  push:
    branches: [main]
    paths: ['conf.d/**']
  workflow_dispatch:

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Validate config
        run: |
          docker run --rm \\
            -v \${{ github.workspace }}/conf.d:/data/conf.d:ro \\
            ghcr.io/vencil/da-tools:latest \\
            validate-config --config-dir /data/conf.d --ci

  generate:
    needs: validate
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Prepare output directory
        run: mkdir -p .output
      - name: Generate routes
        run: |
          docker run --rm \\
            -v \${{ github.workspace }}/conf.d:/data/conf.d:ro \\
            -v \${{ github.workspace }}/.output:/data/output \\
            ghcr.io/vencil/da-tools:latest \\
            generate-routes --config-dir /data/conf.d -o /data/output/routes.yaml --validate
      - name: Compute blast radius
        run: |
          docker run --rm \\
            -v \${{ github.workspace }}/conf.d:/data/conf.d:ro \\
            ghcr.io/vencil/da-tools:latest \\
            config-diff --old-dir /data/conf.d.base --new-dir /data/conf.d --format markdown > .output/blast-radius.md

  apply:
    needs: [validate, generate]
    if: github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4${config.deploy === 'kustomize' ? `
      - name: Apply Kustomize
        run: |
          kustomize build kustomize/overlays/prod > /tmp/manifests.yaml
          kubectl apply --dry-run=server -f /tmp/manifests.yaml
          kubectl apply -f /tmp/manifests.yaml` : config.deploy === 'helm' ? `
      - name: Helm upgrade
        run: |
          helm upgrade --install threshold-exporter \\
            oci://ghcr.io/vencil/charts/threshold-exporter \\
            -f environments/prod/values.yaml \\
            -n monitoring --wait` : `
      - name: Trigger ArgoCD sync
        run: argocd app sync dynamic-alerting --force`}`;
}

window.__cicdGenerateInitCommand = cicdGenerateInitCommand;
window.__cicdGenerateDockerCommand = cicdGenerateDockerCommand;
window.__cicdGenerateFileTree = cicdGenerateFileTree;
window.__cicdGenerateGitHubActionsPreview = cicdGenerateGitHubActionsPreview;

// TD-030e: ESM exports. Removed in TD-030z.
// <!-- jsx-loader-compat: ignore -->
export { cicdGenerateInitCommand, cicdGenerateDockerCommand, cicdGenerateFileTree, cicdGenerateGitHubActionsPreview };
