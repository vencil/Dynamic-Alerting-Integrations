---
title: "CI/CD Setup Wizard — command + config generators"
purpose: |
  Pure functions that build the artifacts the wizard ships back to
  the user: `da-tools init` command, equivalent docker run command,
  generated repo file tree, and a sample GitHub Actions YAML.

  Pre-PR-portal-10 these were inline at the top of
  cicd-setup-wizard.jsx. Splitting drops ~115 LOC from the orchestrator and
  matches the operator-setup-wizard pattern from PR-portal-4.

  Public API:
    cicdGenerateInitCommand(config)            build da-tools CLI
    cicdGenerateDockerCommand(config)          docker wrapper
    cicdGeneratedPaths(config)                 paths `init` will write
    cicdGenerateFileTree(config)               ASCII repo tree
    cicdGenerateGitHubActionsPreview(config)   sample workflow YAML

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

// ⛔ `--user` is load-bearing, and this is the FIRST command a customer
// copies. The image ends `USER nonroot:nonroot` (uid 10001, see
// components/da-tools/app/Dockerfile), while the directory being mounted is
// the customer's own checkout (typically uid 1000). Without `--user`, `init`
// — which creates conf.d/, the CI workflow and the deploy tree — cannot write
// into /workspace and dies on a bare Python traceback (`PermissionError:
// '/workspace/conf.d'`, 0 files written). Measured inside one Linux container:
// uid 10001 -> PermissionError, uid 1000 -> writes fine.
//
// The CLI leg of this same generator already carries the flag; this hand-kept
// twin did not, which is #1351's divergence showing up as a customer-visible
// failure rather than as drift.
function cicdGenerateDockerCommand(config) {
  const init = cicdGenerateInitCommand(config);
  return `docker run --rm -it \\\n  --user $(id -u):$(id -g) \\\n  -v "$(pwd):/workspace" -w /workspace \\\n  ghcr.io/vencil/da-tools:latest \\\n  ${init.replace('da-tools ', '')}`;
}

// ⛔ This list is a CLAIM about another program's behaviour, so it is held to
// that program: tests/ops/test_generated_ci_artifacts.py runs the real
// `run_init()` for each --deploy value and asserts the resulting file set
// equals this one. Keep it derived from what `da-tools init` WRITES, never
// from what a deploy method conceptually needs.
//
// Prior to #1347 this function promised `kustomize/` AND `argocd/` for
// deploy=argocd. `init_project.py` writes NEITHER: both the kustomize tree
// (run_init step 3) and every other deploy artifact are gated on
// `deploy == 'kustomize'`, so the argocd branch scaffolds no deployment files
// at all. The shipped argocd apply stage nonetheless runs
// `argocd app sync dynamic-alerting`, an Application this wizard told the user
// they would receive. That product gap is out of scope here (see the
// "explicitly NOT covered" section of the Python guard); what is fixed is the
// wizard claiming files that never arrive.
function cicdGeneratedPaths(config) {
  const paths = ['conf.d/_defaults.yaml'];
  for (const tenant of config.tenants) {
    paths.push(`conf.d/${tenant}.yaml`);
  }
  if (config.ci === 'github' || config.ci === 'both') {
    paths.push('.github/workflows/dynamic-alerting.yaml');
  }
  if (config.ci === 'gitlab' || config.ci === 'both') {
    paths.push('.gitlab-ci.d/dynamic-alerting.yml');
    // #1357 — the root shell. GitLab auto-loads `.gitlab-ci.yml` and nothing
    // else, so the file above is inert until something includes it; `init`
    // writes this one-line include for you.
    //
    // ⚠️ Conditional in the CLI, unconditional here, and that is the honest
    // shape rather than a bug: `init` skips it when the repo ALREADY has a
    // root `.gitlab-ci.yml` (it never edits a pipeline it did not write, and
    // prints the include for you to paste instead). This wizard has no view
    // of the target repo, so it describes the greenfield run — which is also
    // what the cross-check test compares against, since that fixture
    // initialises into an empty directory. The wizard's Next Steps carry the
    // brownfield caveat in prose.
    paths.push('.gitlab-ci.yml');
  }
  if (config.deploy === 'kustomize') {
    paths.push('kustomize/base/kustomization.yaml');
    paths.push('kustomize/base/README.md');
    paths.push('kustomize/overlays/dev/kustomization.yaml');
    paths.push('kustomize/overlays/prod/kustomization.yaml');
  }
  paths.push('.pre-commit-config.da.yaml');
  paths.push('.da-init.yaml');
  return paths;
}

// Render, not author: every name comes from cicdGeneratedPaths, so a directory
// can never appear in the tree without a file that puts it there. The previous
// hand-drawn version emitted `└──` for EVERY tenant (a malformed tree from the
// second tenant on) because each line carried its own connector glyph.
function _cicdTreeLines(node, prefix) {
  const lines = [];
  const names = Object.keys(node);
  names.forEach((name, i) => {
    const isLast = i === names.length - 1;
    const children = node[name];
    const isDir = children !== null;
    lines.push(`${prefix}${isLast ? '└── ' : '├── '}${name}${isDir ? '/' : ''}`);
    if (isDir) {
      lines.push(..._cicdTreeLines(children, `${prefix}${isLast ? '    ' : '│   '}`));
    }
  });
  return lines;
}

function cicdGenerateFileTree(config) {
  const root = {};
  for (const path of cicdGeneratedPaths(config)) {
    const segments = path.split('/');
    let cursor = root;
    segments.forEach((segment, i) => {
      if (i === segments.length - 1) {
        cursor[segment] = null;
      } else {
        cursor[segment] = cursor[segment] || {};
        cursor = cursor[segment];
      }
    });
  }
  return ['your-repo/', ..._cicdTreeLines(root, '')].join('\n');
}

// ⛔ `apply` must NOT declare `needs: generate` (#1356). `generate` is
// pull_request-only and `apply` is workflow_dispatch-only; GitHub skips every
// job that needs a SKIPPED job, so the two together left `apply` with zero
// reachable events. The CLI generator (scripts/tools/ops/init_project.py,
// _build_github_apply_stage) carries the identical constraint — this preview
// is a second hand-written copy of that workflow (divergence tracked as
// #1351), so a fix to one that is not applied to the other deepens the split.
// Both are held by the reachability assertion in
// tests/ops/test_generated_ci_artifacts.py.
function cicdGenerateGitHubActionsPreview(config) {
  return `name: Dynamic Alerting CI/CD
on:
  pull_request:
    paths: ['conf.d/**']
  # No branches: filter. on.push.branches takes literals only, so any value
  # here guesses the customer's default branch and is wrong for master/trunk
  # repos. Omitting it is correct everywhere and only adds runs - a push to
  # the default branch is still a push. Same reasoning as the CLI generator.
  push:
    paths: ['conf.d/**']
  workflow_dispatch:

# Least-privilege. This preview's generate job writes .output/ and nothing
# else, so read is all it needs. The CLI generator (da-tools init) additionally
# posts a sticky PR comment and therefore also declares pull-requests: write —
# the difference is real, not drift, and granting a write scope this sample
# never uses would be teaching the wrong default.
# (No backticks in this block — it lives inside a JS template literal.)
permissions:
  contents: read

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
            validate-config --config-dir /data/conf.d

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
    needs: [validate]
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

export { cicdGenerateInitCommand, cicdGenerateDockerCommand, cicdGeneratedPaths, cicdGenerateFileTree, cicdGenerateGitHubActionsPreview };
