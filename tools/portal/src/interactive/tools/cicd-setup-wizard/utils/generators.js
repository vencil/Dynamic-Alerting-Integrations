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

// ⛔ There is deliberately no argocd CLI image pin here any more, and none in
// the CLI leg either: `--deploy argocd` is retired (#1351). It scaffolded no
// ArgoCD Application while its apply stage synced one, so what we handed a
// customer could not run as generated — and the only way to make it run was for
// us to pick their ArgoCD conventions. Argo CD remains a supported ENVIRONMENT;
// point your own Application at what `--deploy kustomize` writes.

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
// ⚠️ Kept as a record rather than trimmed: prior to #1347 this function
// promised `kustomize/` AND `argocd/` for deploy=argocd, while `init_project.py`
// wrote NEITHER — the shipped argocd apply stage synced an Application the
// wizard had told the user they would receive. #1347 fixed the wizard's claim;
// the product gap under it was closed the other way in #1351, by retiring
// `--deploy argocd` instead of inventing the Application. Both halves are gone
// now, and the lesson this comment is here for is not: this list is a CLAIM
// about another program's behaviour, so keep it derived from what
// `da-tools init` WRITES, never from what a deploy method conceptually needs.
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
    // configMapGenerator instead).
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

// The trees this workflow watches, and the ONE place the wizard states them.
// ⛔ Derived from the deploy method, not a fixed list: the CLI leg computes the
// same set in `_ci_trigger_trees` (scripts/tools/ops/init_project.py), because
// both directions of a wrong filter are silent (issue 1473) — a filter naming a
// tree nothing writes can never match, while a tree a job READS that is absent
// from the filter means editing it starts no run and the pull request goes green
// having validated nothing.
//
// ⚠️ The CLI has a case this wizard cannot reach: `--config-source git` adds
// `kustomize/**` for any deploy method. The wizard offers no GitOps Native
// toggle, so there is nothing to render for it — and the drift gate compares
// against a CLI run made with the wizard's own settings, so the absence cannot
// quietly become a disagreement.
function _cicdTriggerTrees(config) {
  const trees = ['conf.d'];
  if (config.deploy === 'kustomize') trees.push('kustomize');
  // issue 1454 B — helm's apply step reads environments/prod/values.yaml, so an
  // edit there must start this pipeline.
  if (config.deploy === 'helm') trees.push('environments');
  trees.push('rule-packs');
  return trees.sort();
}

// ⛔ WHAT THIS FUNCTION RETURNS: the file `da-tools init --ci github` writes,
// byte for byte, for the same settings — plus at most ONE comment line of the
// wizard's own (the floating-tag note below). The wizard labels the block with
// the exact filename `.github/workflows/dynamic-alerting.yaml`, so anything
// less is a claim about a file the customer will not get. #1351 recorded what
// the older, hand-shortened version of this preview was worth: first nine
// divergent step lists, then — once those converged — a workflow-level `env:`
// block, a different `name:`, and four step bodies, one of which dropped the
// "conf.d does not exist in this commit" refusal the CLI puts in front of the
// blast-radius diff. owner ruling (a) on #1351 (2026-09-25): the preview is
// the complete, paste-ready file.
//
// ⛔ The text below is transcribed from scripts/tools/ops/init_project.py's
// output (`_gen_github_actions`), and it is still a SECOND copy — the two
// generators stay two (#1351 待辦 1). What holds them together is
// tests/ops/test_generated_ci_artifacts.py, which runs the real `run_init()`
// and requires this function's output, with the one note line removed, to
// EQUAL that file: every byte, comments included. So a change to the CLI
// template turns that test red until this copy is updated to match; regenerate
// rather than hand-edit (render both deploy methods with run_init and replace
// the static text, keeping the five placeholders below).
//
// ⛔ `apply` must NOT declare `needs: generate` (#1356): `generate` is
// pull_request-only and `apply` is workflow_dispatch-only, and GitHub skips
// every job that needs a SKIPPED job. That constraint now lives in the
// transcribed text itself.
//
// The five placeholders, and nothing else, vary:
//   * `triggerPaths`  — the trees that start the pipeline (`_cicdTriggerTrees`)
//   * `image`         — the DA_TOOLS_IMAGE value; every `docker run` refers to
//                       it as `${{ env.DA_TOOLS_IMAGE }}`, so it appears ONCE
//   * `pinNote`       — the wizard's floating-tag note (see below)
//   * the Stage 3 header comment and the apply steps, per deploy method
// CONFIG_DIR and MONITORING_NS are the CLI's defaults (`conf.d`, `monitoring`),
// which is also what the wizard's own `da-tools init` command line asks for.
const _CICD_APPLY_BLOCKS = {
  kustomize: {
    header: `  # ── Stage 3: Apply (manual trigger only) ──────────────
`,
    steps: `      - name: Build ConfigMaps via Kustomize
        run: |
          # --load-restrictor: conf.d files are symlinked into
          # kustomize/base/, and the default restrictor refuses a
          # symlink whose target sits outside that directory.
          kustomize build --load-restrictor LoadRestrictionsNone "kustomize/overlays/prod" > /tmp/manifests.yaml
      - name: Apply to cluster (dry-run first)
        run: |
          kubectl apply --dry-run=server -f /tmp/manifests.yaml
          echo "--- Dry-run passed. Applying... ---"
          kubectl apply -f /tmp/manifests.yaml
      - name: Reload Prometheus
        run: |
          kubectl rollout restart deployment/prometheus -n \${{ env.MONITORING_NS }}
`,
  },
  helm: {
    header: `  # ── Stage 3: Apply via Helm (manual trigger only) ─────
`,
    steps: `      - name: Helm upgrade threshold-exporter
        run: |
          helm upgrade --install threshold-exporter \\
            oci://ghcr.io/vencil/charts/threshold-exporter \\
            -f "environments/prod/values.yaml" \\
            -n \${{ env.MONITORING_NS }} \\
            --wait --timeout 5m
`,
  },
};

function cicdGenerateGitHubActionsPreview(config) {
  // ⛔ Refuse an unknown deploy method instead of rendering around it. The CLI
  // leg's builders raise for the same input; this is that refusal on this side
  // of the pair.
  if (config.deploy !== 'kustomize' && config.deploy !== 'helm') {
    throw new Error(`cicdGenerateGitHubActionsPreview: unknown deploy method ${JSON.stringify(config.deploy)} — expected 'kustomize' or 'helm'. A new deploy method needs an apply block here AND in scripts/tools/ops/init_project.py.`);
  }
  const APPLY = _CICD_APPLY_BLOCKS;
  const image = cicdDaToolsImage(config);
  // ⚠️ Emitted only when the reference can actually be repointed, because this
  // YAML is a file the customer pastes into their own repo: telling a reader
  // who pinned a digest that their image "can change" ships a false statement
  // into their tree. One sentence covers both remaining cases truthfully — our
  // :latest, which we move on purpose, and a foreign tag, whose registry we
  // cannot speak for — so there is only ever one claim to keep true. It is the
  // ONE line this preview adds to what `da-tools init` writes, and the drift
  // gate removes exactly this line before comparing.
  const pinNote = cicdImageIsMutable(image)
    ? `# ${image} can be repointed at different code without this file changing - :latest moves by design, any other tag at its registry's discretion. Pin a digest if this pipeline has to be reproducible.\n`
    : '';
  const triggerPaths = _cicdTriggerTrees(config).map((t) => `      - '${t}/**'`).join('\n');
  return `# Dynamic Alerting CI/CD Pipeline
# Generated by: da-tools init
# Docs: https://vencil.github.io/Dynamic-Alerting-Integrations/scenarios/gitops-ci-integration/
#
# Three stages:
#   1. Validate: Schema + routing guardrails + domain policy
#   2. Generate: Alertmanager routes + blast radius diff (PR comment)
#   3. Apply:    Deploy to cluster (manual trigger only)

name: Dynamic Alerting

on:
  pull_request:
    paths:
${triggerPaths}
  # ⛔ No \`branches:\` filter, deliberately. \`on.push.branches\` takes literals
  # only — no expressions — so any value we write here is a guess about the
  # customer's default branch, and \`main\` is wrong for every \`master\` /
  # \`trunk\` / \`develop\` repo. Enumerating the common names is the same
  # denylist mistake one size larger. Omitting the filter is the only form
  # that is correct for everyone, and it can only ADD runs: a push to the
  # default branch is still a push, so the post-merge re-validation this leg
  # exists for is unchanged. The extra runs are \`validate\` alone (\`generate\`
  # is pull_request-only, \`apply\` is workflow_dispatch-only) — a read-only
  # \`docker run validate-config\` with no credentials, already narrowed by
  # the paths filter below. The GitLab leg gets portability from
  # \`$CI_DEFAULT_BRANCH\`; this is the GitHub equivalent.
  # ⛔ The SAME trees as the pull_request leg — one rendered list
  # substituted twice, so the two cannot drift apart by editing one. It used
  # to list \`conf.d/**\` alone, so a direct push touching only
  # \`rule-packs/custom/**\` ran nothing — and the custom-rule governance lint
  # inside \`validate\` is scoped to exactly that tree. A repo that permits
  # direct pushes got no lint at all on tenant-authored PromQL pushed that
  # way.
  # ⛔ WHICH trees depends on this invocation's \`--deploy\` and
  # \`--config-source\`, and both directions of getting that wrong are
  # silent (issue 1473): a filter naming a tree nothing here writes and no
  # job reads can never match, while a tree a job DOES read — helm's
  # \`environments/prod/values.yaml\` — that is absent from the filter means
  # editing it creates no job at all and the pull request goes green having
  # validated nothing.
  push:
    paths:
${triggerPaths}
  workflow_dispatch:

# Least-privilege, and \`pull-requests: write\` is LOAD-BEARING, not
# boilerplate: the generate job's only output is a sticky PR comment, and
# GITHUB_TOKEN defaults to read-only on repositories created after 2023-02.
# Without this, Stage 2's comment step 403s and the customer's blast-radius
# review never appears. Same declaration this platform's own backtest.yaml
# carries for the same action.
#
# ⛔ SCOPE — on a pull_request from a FORK this block is not enough on its
# own, and the reason is a repo SETTING rather than a hard platform lock.
# By default a fork PR's GITHUB_TOKEN is read-only no matter what
# \`permissions:\` asks for, so the comment step 403s. The documented
# exception is the admin toggle "Send write tokens to workflows from pull
# requests" — which lives under the settings for forks of PRIVATE
# repositories, i.e. exactly the shape most customers deploy this in. With
# that toggle on, this \`permissions:\` block is what grants the write, so it
# is load-bearing in that configuration too.
#
# We deliberately do NOT use \`pull_request_target\` / \`workflow_run\` to buy
# the elevated token, because both run trusted code against untrusted input.
# So: same-repo branches work as-is; fork PRs need the customer's admin to
# make that call knowingly. Stated here rather than silently implied either
# way — the previous wording claimed the platform hard-locks this and that
# a different design was required, which is not what the docs say.
#
# ⛔ The write scope sits on the \`generate\` job, NOT here. At workflow level
# every job inherits it, including \`apply\` — the one carrying
# \`environment: production\` and cluster credentials, which posts no comment
# and needs no PR write. This is the shape this platform uses on itself in
# 6 workflows / 9 jobs (bench-on-demand.yaml's \`gate\` job is identical:
# \`contents: read\` at the top, \`pull-requests: write\` on the one job that
# comments).
permissions:
  contents: read

env:
  DA_TOOLS_IMAGE: ${image}
  CONFIG_DIR: conf.d
  MONITORING_NS: monitoring

${pinNote}jobs:
  # ── Stage 1: Validate ─────────────────────────────────
  validate:
    runs-on: ubuntu-latest
    # A job with no timeout gets GitHub's default of 6 hours. Nothing here
    # takes minutes: it is one \`docker run\` per step. A wedged image pull or
    # a hung container therefore holds a runner for the rest of the working
    # day instead of failing while somebody is still looking at the PR.
    timeout-minutes: 10
    concurrency:
      # Two pushes in a row used to run this twice, in parallel, with no
      # ordering between them. Validation is read-only and idempotent, so
      # the older run has nothing to contribute once a newer commit exists.
      group: dynamic-alerting-validate-\${{ github.event.pull_request.number || github.ref }}
      cancel-in-progress: true
    steps:
      - uses: actions/checkout@v6

      - name: Validate config (schema + routing + policy)
        run: |
          # ⛔ The same guard the config-diff step carries, and for the same
          # reason: \`docker -v\` CREATES a missing host path instead of
          # failing, so a wrong or moved CONFIG_DIR mounts an EMPTY
          # directory, \`validate-config\` parses zero files and exits 0.
          # Stage 2 refuses to compare in that case; Stage 1 went green and
          # silent — the worse half, because nothing on the PR hints at it.
          if [ ! -d "\${{ env.CONFIG_DIR }}" ]; then
            echo "::error::CONFIG_DIR is set to '\${{ env.CONFIG_DIR }}', which does not exist in this commit. Refusing to validate, because mounting a path that is not there yields an empty directory and a PASS that checked nothing."
            exit 1
          fi
          docker run --rm \\
            -v "\${{ github.workspace }}/\${{ env.CONFIG_DIR }}:/data/conf.d:ro" \\
            \${{ env.DA_TOOLS_IMAGE }} \\
            validate-config --config-dir /data/conf.d

      - name: Lint custom rules (if any)
        run: |
          if [ -d "rule-packs/custom" ]; then
            docker run --rm \\
              -v "\${{ github.workspace }}/rule-packs/custom:/data/rules:ro" \\
              \${{ env.DA_TOOLS_IMAGE }} \\
              lint /data/rules --ci
          fi

  # ── Stage 2: Generate routes + blast radius ────────────
  generate:
    # ⛔ NO \`needs:\` — deliberately, and this is a fix rather than an
    # omission. It used to be \`needs: validate\`, and the \`if:\` below carries
    # no status function, so an implicit \`success()\` applied: any red in
    # \`validate\` skipped this whole job. \`validate\` lints custom Prometheus
    # rules under the rule-packs tree, which has NO causal relationship to
    # the tenant-config blast radius — so one unrelated lint ERROR anywhere
    # in that tree took the blast-radius comment away from EVERY config pull
    # request in the repository until somebody fixed it, and what reviewers
    # saw meanwhile was the previous run's report, which looks current.
    # This platform's own config-diff.yaml computes its blast radius with no
    # \`needs:\` at all, for the same reason.
    # ⚠️ This is not "validation no longer gates anything": \`apply\` still
    # needs \`validate\`, and that is the edge that protects the cluster.
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    # ⛔ The OUTER bound, and it has to stay above the sum of the step caps
    # below (6+1+5+5+5+2+2 = 26). If the job cap fires first the run is
    # CANCELLED, and every \`!cancelled()\` step — the fallback report and the
    # comment — is skipped: the issue 1421 stale comment, reached through a
    # timeout instead of through a failing step. Which is also why EVERY
    # step below carries its own cap, including the cheap ones: an uncapped
    # step has no way to end except by taking the job cap with it. Held by
    # \`test_step_timeouts_leave_room_under_the_job_timeout\`, which grades
    # both halves — no uncapped step, and the sum under this number.
    timeout-minutes: 30
    concurrency:
      # ⛔ Load-bearing for the COMMENT, not just for runner minutes. The
      # sticky comment is edited in place under a fixed header, so two runs
      # of this job race to overwrite the same comment body and the loser is
      # whichever finishes LAST — not whichever commit is newer. Push twice
      # quickly and the PR can end up showing the older commit's blast
      # radius, with nothing in the comment to say so.
      group: dynamic-alerting-blast-radius-\${{ github.event.pull_request.number || github.ref }}
      cancel-in-progress: true
    # The only job that writes anything back to the PR. A job-level block
    # REPLACES the workflow one rather than merging, so \`contents: read\` is
    # restated here — dropping it would 403 the checkout.
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v6
        # The one step here whose honest cap is not small — a full-history
        # clone of a large repository takes minutes. It is still capped:
        # see the job cap above for what an uncapped step costs.
        timeout-minutes: 6
        with:
          # Load-bearing. The blast radius is computed against the PR's
          # base commit, and checkout's default (fetch-depth: 1) does not
          # put that object in the clone — the lookup below then fails and
          # the report silently degrades to "every tenant is new", which
          # docs/internal/lint-policy.md names as pretending to be
          # diff-aware. Same value and same reason as this platform's own
          # config-diff.yaml and blast-radius.yml — but note those reach
          # for \`origin/<base branch>\`, which this depth guarantees,
          # while a PR event's base SHA is only reachable while some
          # branch still leads to it. The next step reports that case
          # rather than papering over it.
          fetch-depth: 0

      - name: Prepare output directory
        timeout-minutes: 1
        run: mkdir -p .output

      - name: Generate Alertmanager routes
        id: routes
        # ⛔ Step caps, and the arithmetic is the point: every step in this
        # job is capped and the caps sum to 26, under this job's 30, so a hung
        # computation FAILS AS A STEP and the fallback below still runs. If
        # the JOB cap fired first the run would be CANCELLED, and both the
        # fallback and the comment step carry \`!cancelled()\` — correct for a
        # superseded run, but it would leave the previous report standing
        # with nothing to say the new run died. Held by
        # \`test_step_timeouts_leave_room_under_the_job_timeout\`.
        timeout-minutes: 5
        run: |
          # Read-only, deliberately: this step VALIDATES the routes it
          # would generate; nothing downstream consumes a written file.
          # An earlier template mounted \`.output\` writable and asked for
          # an output file together with \`--validate\` — but \`--validate\`
          # returns before any file is written, so nothing ever landed
          # there, and the writable mount was only a permission surface
          # (the image runs as a non-root user, the directory belongs to
          # the runner). The tool now refuses an output path under
          # \`--validate\` (exit 2), so that pairing cannot come back
          # quietly. With no writable mount there is nothing for a
          # \`--user\` override to fix; the config-diff step below writes
          # on the HOST via a shell redirect.
          docker run --rm \\
            -v "\${{ github.workspace }}/\${{ env.CONFIG_DIR }}:/data/conf.d:ro" \\
            \${{ env.DA_TOOLS_IMAGE }} \\
            generate-routes --config-dir /data/conf.d --validate

      - name: Resolve base config snapshot
        id: snapshot
        timeout-minutes: 5
        if: github.event_name == 'pull_request'
        env:
          # Passed through env rather than interpolated into the script, so
          # the expression cannot become shell syntax. Same shape as this
          # platform's config-diff.yaml.
          BASE_SHA: \${{ github.event.pull_request.base.sha }}
        run: |
          # Two DIFFERENT conditions used to collapse into a single
          # \`|| mkdir -p\` fallback here, and both produced an empty
          # baseline that exits 0:
          #   (a) the base commit is absent from the clone -> a fault; the
          #       comparison is impossible and must not be faked.
          #   (b) the base commit is present but carried no config
          #       directory yet -> a genuine first import, where "every
          #       tenant is new" is the correct answer.
          # Telling them apart is the whole reason this is two lookups
          # instead of one \`||\` chain.
          # RUNNER_TEMP is a default runner variable, and assignments to
          # those are ignored, so this should be unreachable on a hosted
          # runner — it is a guard for anything replaying these steps
          # elsewhere. Fail loudly rather than fall back:
          # Measured with it unset: as a normal user the step dies on a
          # bare \`/base.tar: Permission denied\` with no ::error:: to
          # explain it, and as root it SUCCEEDS while writing five stray
          # files into the filesystem root.
          : "\${RUNNER_TEMP:?RUNNER_TEMP is not set; this step writes its intermediate files there}"
          # Trailing slash matters: \`git ls-tree -- conf.d/\` lists the
          # directory's CHILDREN, while \`-- conf.d\` returns the entry
          # itself. Reading a type out of the first form yields the types
          # of the children, so a healthy repository whose CONFIG_DIR was
          # written the natural way ("a directory, so it ends in /") was
          # rejected as "a blob, not a directory".
          config_dir="\${CONFIG_DIR%/}"
          mkdir -p .output/base/"$config_dir"
          if ! git cat-file -e "$BASE_SHA" 2>/dev/null; then
            echo "::error::base commit $BASE_SHA is not in this clone, so there is nothing to compare against. Two causes: the checkout was narrowed (this workflow sets fetch-depth: 0 — check it is still there), or the base ref was rewritten and that commit no longer exists, which is what a force-push, or re-running an old job whose recorded base commit is gone, looks like."
            exit 1
          fi
          # Read the entry from its PARENT tree rather than asking about
          # the object at that path. \`git cat-file -t "$BASE_SHA:$dir"\`
          # looks up the object the entry POINTS AT, and for a submodule
          # that is a commit belonging to another repository — absent
          # here, so it fails, and the fault would be filed as "no config
          # directory at the base", i.e. a first import. Measured: a
          # gitlink took the first-import branch and exited 0 with an
          # empty baseline. \`ls-tree\` reads the entry itself, so it can
          # say "commit" without ever resolving it.
          git ls-tree "$BASE_SHA" -- "$config_dir" > "$RUNNER_TEMP"/entry.txt
          kind=$(cut -d' ' -f2 "$RUNNER_TEMP"/entry.txt)
          if [ -z "$kind" ]; then kind=missing; fi
          if [ "$kind" = tree ]; then
            # ⛔ Extracted through a scratch index, NOT through
            # \`git archive\`. \`export-ignore\` is an ARCHIVE-ONLY attribute
            # (git: "won't be added to archive files"), so a
            # .gitattributes rule covering the config directory makes
            # \`git archive\` emit a partial or empty baseline while every
            # command reports success — and the diff then overstates or
            # invents changes.
            #
            # Three separate attempts to DETECT that after the fact were
            # each falsified by measurement: a file count disagreed with
            # \`find\` over symlinks; a name set disagreed with the escaping
            # \`core.quotePath\` applies to non-ASCII, quote and backslash
            # paths; and "did anything arrive at all" was satisfied by a
            # stray README while all three tenant files were missing.
            # Reading the tree into an index and checking it out removes
            # the failure mode instead of watching for it — no comparison,
            # nothing to enumerate, and no pipeline to swallow a failure.
            GIT_INDEX_FILE="$RUNNER_TEMP"/base.idx git read-tree "$BASE_SHA:$config_dir"
            GIT_INDEX_FILE="$RUNNER_TEMP"/base.idx git checkout-index -a -f --prefix=.output/base/"$config_dir"/
          elif [ "$kind" = missing ]; then
            echo "::notice::$config_dir does not exist at $BASE_SHA; treating this as the first import, so every tenant is reported as added"
          else
            echo "::error::$config_dir at $BASE_SHA is a $kind, not a directory, so no baseline can be built from it. A kind of 'commit' means a submodule is mounted there; 'blob' means either a file has that name, or the path is a symlink (git records those as blobs too). Reporting any of those as a first import would hide the fault."
            exit 1
          fi

      - name: Config diff (blast radius)
        id: diff
        timeout-minutes: 5
        run: |
          # config-diff signals findings through its exit code, so a bare
          # call cannot work: "changed" is exit 1, and this job runs on
          # every pull request that touches ANY tree in this workflow's
          # \`on.pull_request.paths\` above (not conf.d/ alone) — so both 0
          # and 1 are ordinary outcomes here.
          #   0 = no config change  -> the report says so in words, and the
          #       comment below is refreshed with it. Not skipped: this job
          #       also runs for edits to the other watched trees, which do
          #       not touch tenant config at all, and skipping would leave
          #       the PREVIOUS run's report standing as though it were
          #       still current.
          #       ⚠️ READ THIS BEFORE TRUSTING A "no changes" COMMENT.
          #       config-diff compares TENANT files only — it skips every
          #       file whose name starts with \`_\`, which includes
          #       conf.d/_defaults.yaml. So a pull request that changes a
          #       PLATFORM DEFAULT, inherited by every tenant that does
          #       not override it, gets a comment that states "No changes
          #       detected". Measured: raising _defaults.yaml's
          #       mysql_connections exits 0 with a 325-byte "no changes"
          #       report, while the same key changed in one tenant file
          #       exits 1 and is listed. That is the widest-blast-radius
          #       edit this job can be handed, and it is the one it cannot
          #       see. Review _defaults.yaml changes by hand, or gate them
          #       separately (the platform runs its own
          #       guard-defaults-impact workflow for exactly this, which
          #       \`da-tools init\` does not emit).
          #   1 = changes detected  -> the report is the payload.
          #   2 and above           -> the run did not complete; fail, and
          #       the step below replaces the report with an explicit
          #       "NOT COMPUTED" note naming which step failed. It used to
          #       post nothing at all (the comment step inherited an
          #       implicit success()), which left the previous run's report
          #       on the pull request looking current.
          #
          # The head-side directory is checked FIRST because a bind mount
          # creates a missing host path instead of failing. config-diff
          # does guard this (\`ERROR: new-dir not found\`), but that guard
          # can never fire through a \`-v\` mount: the path always exists by
          # the time the tool looks. Measured, with CONFIG_DIR pointing at
          # a path that is not in the repo: both sides mount as empty
          # directories, the tool exits 0, and it prints a 322-byte
          # "no changes" report — non-empty, so the empty-report guard
          # below passes it too, and the comment is published. That is a
          # blast-radius gate that is green and silent forever, which is
          # the same failure this whole step was rewritten to remove.
          if [ ! -d "\${{ env.CONFIG_DIR }}" ]; then
            echo "::error::CONFIG_DIR is set to '\${{ env.CONFIG_DIR }}', which does not exist in this pull request's head commit. Either the workflow's CONFIG_DIR does not match where this repository actually keeps its tenant config, or this pull request removed that directory. Refusing to compare, because mounting a path that is not there yields an empty directory and a report that says 'no changes' on every future run."
            exit 1
          fi
          set +e
          # Two read-only mounts only: config-diff writes to stdout and the
          # HOST redirect below lands it in .output/. A writable output
          # mount used to ride along here too, unused.
          docker run --rm \\
            -v "\${{ github.workspace }}/.output/base/\${{ env.CONFIG_DIR }}:/data/conf.d.base:ro" \\
            -v "\${{ github.workspace }}/\${{ env.CONFIG_DIR }}:/data/conf.d:ro" \\
            \${{ env.DA_TOOLS_IMAGE }} \\
            config-diff --old-dir /data/conf.d.base --new-dir /data/conf.d \\
              --format markdown > .output/blast-radius.md
          rc=$?
          set -e
          if [ "$rc" -gt 1 ]; then
            echo "::error::config-diff exited $rc (expected 0 or 1) — image pull, mount, or malformed config"
            exit "$rc"
          fi
          # rc alone is not enough. Anything in front of the tool can exit
          # 1 with nothing on stdout — a mistyped or renamed subcommand
          # leaves the entrypoint exiting 1, which is indistinguishable
          # from "changes detected" by exit code alone, and the comment
          # comment action would then fail with "Either message or path
          # input is required" — an error pointing at an input that WAS
          # supplied, which sends the reader to the wrong place. (It
          # refuses to publish an empty body; what it cannot do is say
          # why.) A one-byte file is worse: that publishes. The tool
          # always prints a report on 0 and on 1, so an empty or
          # near-empty file here means the run did not really happen.
          if [ ! -s .output/blast-radius.md ]; then
            echo "::error::config-diff exited $rc but produced an empty report; treating this as a failed run rather than publishing it"
            exit 1
          fi

      - name: Explain a blast radius that could not be computed
        # ⛔ The point of this step is that the comment below has something
        # CURRENT to post on every path, including the failing ones. The
        # sticky comment is edited in place under a fixed header: when a step
        # above exits non-zero, the comment step used to be skipped by its
        # implicit success() and the PREVIOUS run's report stayed on the pull
        # request, timestamped when it was first posted and with no
        # notification that anything changed. Stale is worse than absent
        # here, because stale looks like the answer. There is a red X on the
        # run, but nothing on the comment itself says it is out of date.
        # \`!cancelled()\` rather than \`always()\`: a run cancelled by the
        # concurrency group above has been SUPERSEDED, and a superseded run
        # must not overwrite the winner's comment.
        if: \${{ !cancelled() }}
        timeout-minutes: 2
        env:
          ROUTES_OUTCOME: \${{ steps.routes.outcome }}
          SNAPSHOT_OUTCOME: \${{ steps.snapshot.outcome }}
          DIFF_OUTCOME: \${{ steps.diff.outcome }}
        run: |
          # The diff step writes the report and then refuses to publish an
          # empty one, so "the file is non-empty AND that step succeeded" is
          # the only state in which the report is this run's own work.
          if [ "$DIFF_OUTCOME" = success ] && [ -s .output/blast-radius.md ]; then
            exit 0
          fi
          mkdir -p .output
          {
            echo "## Blast radius: NOT COMPUTED"
            echo
            echo "This run could not compute the tenant-config blast radius, so this comment replaces the previous run's report. **Nothing here says the change is safe — it says nobody measured it.**"
            echo
            echo "| step | outcome |"
            echo "| --- | --- |"
            echo "| Generate Alertmanager routes | \\\`\${ROUTES_OUTCOME:-did not run}\\\` |"
            echo "| Resolve base config snapshot | \\\`\${SNAPSHOT_OUTCOME:-did not run}\\\` |"
            echo "| Config diff (blast radius) | \\\`\${DIFF_OUTCOME:-did not run}\\\` |"
            echo
            echo "Open the failing step in this run's log: each failure path prints an \\\`::error::\\\` line naming the cause (base commit missing from the clone, a submodule or symlink where the config directory should be, \\\`CONFIG_DIR\\\` pointing at a path this commit does not have, or the tool exiting above 1)."
          } > .output/blast-radius.md

      - name: Post PR comment with blast radius
        # See the step above: skipping this on a failure is what leaves a
        # stale report standing. Both steps share one condition on purpose.
        if: \${{ !cancelled() }}
        # Capped like the rest: this step talks to the GitHub API, and a
        # hang here is the one that would eat the job cap AFTER the report
        # was written but BEFORE it was posted.
        timeout-minutes: 2
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          path: .output/blast-radius.md
          header: dynamic-alerting-blast-radius

${APPLY[config.deploy].header}  # \`needs: [validate]\` only — \`generate\` is pull_request-only, and a
  # skipped job skips everything that needs it (issue 1356).
  apply:
    needs: [validate]
    runs-on: ubuntu-latest
    if: github.event_name == 'workflow_dispatch'
    environment: production
    # Wider than the other two: this one talks to a cluster, and a rollout
    # that is merely slow must not be killed halfway.
    timeout-minutes: 30
    concurrency:
      # ⛔ \`cancel-in-progress: FALSE\`, and the difference from the other two
      # jobs is the whole reason this block is spelled out per job rather
      # than once at workflow level. Cancelling a read-only validate or a
      # superseded diff costs nothing; cancelling this one interrupts
      # \`kubectl apply\` / \`helm upgrade\` partway and leaves the cluster in a
      # state no commit describes.
      # ⛔ The group is keyed on the TARGET NAMESPACE, not on the ref. This
      # job is workflow_dispatch-only and a dispatch can start from any
      # branch, while every one of them deploys to the same namespace — so a
      # ref-keyed group puts two dispatches in DIFFERENT groups and lets
      # them run \`kubectl apply\` / \`helm upgrade\` at the same time, which is
      # the exact interleaving this block exists to prevent. A job-level
      # \`concurrency\` cannot read \`env:\`, so the namespace is baked in here
      # at generation time.
      group: dynamic-alerting-apply-monitoring
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@v6
${APPLY[config.deploy].steps}`;
}

export { CICD_DEFAULT_DA_TOOLS_IMAGE, cicdDaToolsImage, cicdImageIsMutable, cicdSplitImageRef, cicdGenerateInitCommand, cicdGenerateDockerCommand, cicdGeneratedPaths, cicdGenerateFileTree, cicdGenerateGitHubActionsPreview };
