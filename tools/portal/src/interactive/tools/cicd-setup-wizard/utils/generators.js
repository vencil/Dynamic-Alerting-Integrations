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
    CICD_DEFAULT_DA_TOOLS_IMAGE                default da-tools image ref
    cicdDaToolsImage(config)                   image ref this config resolves to
    cicdImageIsMutable(image)                  can this ref be repointed?
    cicdSplitImageRef(image)                   {repo, tag, digest} of a ref
    cicdGenerateInitCommand(config)            build da-tools CLI
    cicdGenerateDockerCommand(config)          docker wrapper
    cicdGeneratedPaths(config)                 paths `init` will write
    cicdGenerateFileTree(config)               ASCII repo tree
    cicdGenerateGitHubActionsPreview(config)   sample workflow YAML

  Closure deps: none. Pure functions; receive config as arg.
---

// ⛔ The ONE place in the portal that spells the da-tools image. The CLI leg
// keeps its own default in scripts/tools/ops/init_project.py
// (`DA_TOOLS_IMAGE`) and exposes it as `--da-tools-image`; this hand-kept twin
// had the same string typed into four template literals with no way for a
// customer to change it, which is the "da-tools 映像" row of #1351's divergence
// table. Same default as the CLI, deliberately: `cicdGenerators.test.ts` pins
// the literal, so changing it here alone goes red.
const CICD_DEFAULT_DA_TOOLS_IMAGE = 'ghcr.io/vencil/da-tools:latest';

// Empty / whitespace-only falls back rather than emitting `docker run  init`,
// because the field this reads is a free-text input the customer can clear.
function cicdDaToolsImage(config) {
  const raw = config && config.daToolsImage;
  const trimmed = typeof raw === 'string' ? raw.trim() : '';
  return trimmed === '' ? CICD_DEFAULT_DA_TOOLS_IMAGE : trimmed;
}

// Split a reference into {repo, tag, digest}. Deliberately NOT a full OCI
// grammar: the tag separator is the first `:` of the LAST `/`-segment, so a
// colon ahead of that is a registry port (`registry.internal:5000/da-tools`
// carries no tag). A digest makes the tag irrelevant and is reported alone.
//
// ⛔ Exported for its own sake, not for reuse. `cicdImageIsMutable` cannot
// observe the port branch — our registry has no port, so every port-bearing
// reference is foreign and mutable however it is split, and a mutation that
// deleted the `/`-anchoring left all 49 tests green. Reaching the parse
// directly is what turns that from correct-looking code nothing measures into
// something a test can fail on.
function cicdSplitImageRef(image) {
  const at = image.indexOf('@');
  if (at !== -1) return { repo: image.slice(0, at), tag: null, digest: true };
  const slash = image.lastIndexOf('/');
  const name = image.slice(slash + 1);
  const colon = name.indexOf(':');
  if (colon === -1) return { repo: image, tag: null, digest: false };
  return {
    repo: image.slice(0, slash + 1) + name.slice(0, colon),
    tag: name.slice(colon + 1),
    digest: false,
  };
}

// The one repository whose tag policy this project can speak for.
const CICD_DA_TOOLS_REPOSITORY = cicdSplitImageRef(CICD_DEFAULT_DA_TOOLS_IMAGE).repo;

// "Mutable" = the reference can be repointed at different code while the
// workflow file stays byte-identical. Only a digest rules that out.
//
// ⛔ The version-tag exemption is scoped to OUR repository, and that scoping is
// the whole point. components/da-tools/README.md does say "quick-start 用
// :latest 即可；production 請釘特定版號如 :v2.8.0" — but the line under it is
// `docker pull ghcr.io/vencil/da-tools:latest`, so that policy is a promise
// about tags WE publish, not a property of tags. An earlier revision of this
// function read it as the latter and went silent for
// `registry.internal/da-tools:v1`, telling a customer their own registry's tag
// is immutable when nothing here can know that (CWE-494, raised in review on
// #1880). Tags are mutable by default in OCI; the exemption is a statement
// about us, so it may only be applied to us.
//
function cicdImageIsMutable(image) {
  const { repo, tag, digest } = cicdSplitImageRef(image);
  if (digest) return false;
  if (tag === null || tag === 'latest') return true;
  return repo !== CICD_DA_TOOLS_REPOSITORY;
}

function cicdGenerateInitCommand(config) {
  const parts = ['da-tools init'];
  if (config.ci) parts.push(`--ci ${config.ci}`);
  if (config.deploy) parts.push(`--deploy ${config.deploy}`);
  if (config.tenants.length > 0) parts.push(`--tenants ${config.tenants.join(',')}`);
  if (config.packs.length > 0) parts.push(`--rule-packs ${config.packs.join(',')}`);
  // ⛔ The flag is NOT redundant with the image the docker wrapper runs. It
  // decides what `init` WRITES: `--da-tools-image X` is what puts
  // `DA_TOOLS_IMAGE: X` into the .github workflow, the GitLab pipeline and the
  // pre-commit snippet. Measured on a real run of init_project.py — with and
  // without the flag, the generated workflow differs on exactly that line.
  // Without this, a customer who set an image here would read a preview naming
  // their registry and then get `:latest` in the file `init` actually wrote,
  // which is #1351's headline defect ("預覽與 init 寫出的是兩個不同的東西")
  // reintroduced by the very change meant to close that row.
  //
  // Emitted only when it differs from the default, because the CLI's own
  // default IS that value: at the default the flag would be a no-op the
  // customer has to read past.
  const image = cicdDaToolsImage(config);
  if (image !== CICD_DEFAULT_DA_TOOLS_IMAGE) parts.push(`--da-tools-image ${image}`);
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
  return `docker run --rm -it \\\n  --user $(id -u):$(id -g) \\\n  -v "$(pwd):/workspace" -w /workspace \\\n  ${cicdDaToolsImage(config)} \\\n  ${init.replace('da-tools ', '')}`;
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
  if (config.deploy === 'helm') {
    // issue 1454 B. Both apply stages already passed `-f
    // environments/prod/values.yaml` while no code path created it, so the
    // customer's first manual deploy died on `no such file or directory`.
    // `init` now writes a skeleton; helm only, because it is the helm apply
    // step that reads it (kustomize mounts conf.d/ through its
    // configMapGenerator, and argocd reads no repository path at all).
    paths.push('environments/prod/values.yaml');
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
  const image = cicdDaToolsImage(config);
  // ⚠️ Emitted only when the reference can actually be repointed, because this
  // YAML is a file the customer pastes into their own repo: telling a reader
  // who pinned a digest that their image "can change" ships a false statement
  // into their tree. One sentence covers both remaining cases truthfully — our
  // :latest, which we move on purpose, and a foreign tag, whose registry we
  // cannot speak for — so there is only ever one claim to keep true. Not
  // reusable from the prose leg: the warning in
  // docs/scenarios/gitops-ci-integration.md is about the same tag but answers
  // a different question (how to tell whether the artifact you already have
  // carries a fix the doc describes, "不要看版號、直接看產物").
  const pinNote = cicdImageIsMutable(image)
    ? `# ${image} can be repointed at different code without this file changing - :latest moves by design, any other tag at its registry's discretion. Pin a digest if this pipeline has to be reproducible.\n`
    : '';
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

${pinNote}jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Validate config
        run: |
          docker run --rm \\
            -v \${{ github.workspace }}/conf.d:/data/conf.d:ro \\
            ${image} \\
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
          # Validate only (#1423 / #1650): --validate returns before -o is
          # used, and the tool now refuses the two together.
          docker run --rm \\
            -v \${{ github.workspace }}/conf.d:/data/conf.d:ro \\
            ${image} \\
            generate-routes --config-dir /data/conf.d --validate
      - name: Compute blast radius
        run: |
          docker run --rm \\
            -v \${{ github.workspace }}/conf.d:/data/conf.d:ro \\
            ${image} \\
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

export { CICD_DEFAULT_DA_TOOLS_IMAGE, cicdDaToolsImage, cicdImageIsMutable, cicdSplitImageRef, cicdGenerateInitCommand, cicdGenerateDockerCommand, cicdGeneratedPaths, cicdGenerateFileTree, cicdGenerateGitHubActionsPreview };
