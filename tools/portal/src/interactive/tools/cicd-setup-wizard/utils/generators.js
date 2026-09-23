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

// ⛔ `apply` must NOT declare `needs: generate` (#1356). `generate` is
// pull_request-only and `apply` is workflow_dispatch-only; GitHub skips every
// job that needs a SKIPPED job, so the two together left `apply` with zero
// reachable events. The CLI generator (scripts/tools/ops/init_project.py,
// _build_github_apply_stage) carries the identical constraint — this preview
// is a second hand-written copy of that workflow (divergence tracked as
// #1351), so a fix to one that is not applied to the other deepens the split.
// Both are held by the reachability assertion in
// tests/ops/test_generated_ci_artifacts.py.
//
// ⛔ WHAT THIS FUNCTION IS. Not "a sample pipeline" — the wizard labels the
// block with the exact filename `.github/workflows/dynamic-alerting.yaml` and
// says nowhere that it is illustrative, so every line here is a claim about the
// file `da-tools init` writes. #1351 measured what that claim was worth: the
// preview watched one tree where the artifact watches three, and its nine
// job × deploy step lists ALL differed from the artifact's — the preview
// under-reported the CI the customer actually gets (no blast-radius PR comment,
// no custom-rule lint, no kustomize dry-run, no Prometheus reload) and showed
// `argocd app sync --force` where the artifact runs `--prune --timeout 300`,
// two flags whose semantics do not overlap. (That deploy method is retired —
// #1351 — so this particular row can no longer recur; the shape it illustrates
// can, on any pair of branches.)
//
// So: the trigger paths, the job set, the per-job step-name sequence and the
// deploy commands' flags are now the artifact's, and
// tests/ops/test_generated_ci_artifacts.py compares them against a real
// `run_init()` run rather than against a transcription kept here.
//
// ⚠️ ONE declared difference remains, and it is a product decision rather than
// drift (#1351's `env:` row): the artifact declares DA_TOOLS_IMAGE / CONFIG_DIR
// / MONITORING_NS in a workflow-level `env:` block and refers to them as
// GitHub expressions, while this preview inlines the values — the image from
// the wizard's own field, the config directory and namespace at the CLI's
// defaults, which is also what the wizard's `da-tools init` command line asks
// for. The workflow `name:` is the other face of that same row. Both are held
// by the skeleton gate, which pins the difference to exactly `{env}` rather
// than letting a new one ride in beside it.
function cicdGenerateGitHubActionsPreview(config) {
  // ⛔ Refuse an unknown deploy method instead of rendering around it. The apply
  // block is a ternary chain, and when its last arm stopped being argocd
  // (retired, #1351) the natural spelling left `''` there — which emits a job
  // with an EMPTY `steps:`, i.e. valid JavaScript and an invalid workflow. The
  // CLI leg's two builders raise for the same input; this is that refusal on
  // this side of the pair.
  if (config.deploy !== 'kustomize' && config.deploy !== 'helm') {
    throw new Error(`cicdGenerateGitHubActionsPreview: unknown deploy method ${JSON.stringify(config.deploy)} — expected 'kustomize' or 'helm'. A new deploy method needs an apply block here AND in scripts/tools/ops/init_project.py.`);
  }
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
  const paths = `[${_cicdTriggerTrees(config).map((t) => `'${t}/**'`).join(', ')}]`;
  return `name: Dynamic Alerting CI/CD
on:
  pull_request:
    paths: ${paths}
  # No branches: filter. on.push.branches takes literals only, so any value
  # here guesses the customer's default branch and is wrong for master/trunk
  # repos. Omitting it is correct everywhere and only adds runs - a push to
  # the default branch is still a push. Same reasoning as the CLI generator.
  push:
    paths: ${paths}
  workflow_dispatch:

# Least-privilege at the workflow level; the one job that writes back to the
# pull request raises its own scope below. A job-level block REPLACES this one
# rather than merging, which is why that job restates contents: read.
# (No backticks in this block — it lives inside a JS template literal.)
permissions:
  contents: read

${pinNote}jobs:
  validate:
    runs-on: ubuntu-latest
    # A job with no timeout gets GitHub's default of 6 hours, and nothing here
    # takes minutes. Validation is read-only, so a superseded run has nothing
    # left to contribute.
    timeout-minutes: 10
    concurrency:
      group: dynamic-alerting-validate-\${{ github.event.pull_request.number || github.ref }}
      cancel-in-progress: true
    steps:
      - uses: actions/checkout@v6
      - name: Validate config (schema + routing + policy)
        run: |
          # docker -v CREATES a missing host path instead of failing, so a
          # wrong config directory mounts an EMPTY one, validate-config parses
          # zero files and exits 0. Refuse instead of passing silently.
          if [ ! -d "conf.d" ]; then
            echo "::error::conf.d does not exist in this commit. Refusing to validate, because mounting a path that is not there yields a PASS that checked nothing."
            exit 1
          fi
          docker run --rm \\
            -v "\${{ github.workspace }}/conf.d:/data/conf.d:ro" \\
            ${image} \\
            validate-config --config-dir /data/conf.d
      - name: Lint custom rules (if any)
        run: |
          if [ -d "rule-packs/custom" ]; then
            docker run --rm \\
              -v "\${{ github.workspace }}/rule-packs/custom:/data/rules:ro" \\
              ${image} \\
              lint /data/rules --ci
          fi

  generate:
    # No needs: on purpose. It used to be needs: validate, and the if: below
    # carries no status function, so an implicit success() applied - one lint
    # ERROR in the custom rule-packs tree, which has nothing to do with the
    # tenant-config blast radius, took this comment away from every config pull
    # request in the repository. apply still needs validate; that is the edge
    # that protects the cluster.
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    # Above the sum of the step caps below, or it fires first and cancels.
    timeout-minutes: 20
    concurrency:
      # The sticky comment is edited in place under a fixed header, so two runs
      # race to overwrite the same body and the LAST to finish wins - not the
      # newer commit.
      group: dynamic-alerting-blast-radius-\${{ github.event.pull_request.number || github.ref }}
      cancel-in-progress: true
    # Load-bearing, not boilerplate: the blast-radius comment is this job's
    # only output, and GITHUB_TOKEN defaults to read-only on repositories
    # created after 2023-02. The scope sits on this job alone so that apply,
    # which carries the production environment, never inherits it.
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v6
        with:
          # The blast radius is computed against the pull request's base
          # commit, which a shallow clone does not contain.
          fetch-depth: 0
      - name: Prepare output directory
        run: mkdir -p .output
      - name: Generate Alertmanager routes
        id: routes
        # 3 x 5 stays under this job's 20, so a hung step FAILS instead of
        # the job being cancelled - a cancelled run skips the !cancelled()
        # fallback below and leaves the previous report standing.
        timeout-minutes: 5
        run: |
          # Validate only (#1423 / #1650): --validate returns before -o is
          # used, and the tool now refuses the two together.
          docker run --rm \\
            -v "\${{ github.workspace }}/conf.d:/data/conf.d:ro" \\
            ${image} \\
            generate-routes --config-dir /data/conf.d --validate
      - name: Resolve base config snapshot
        id: snapshot
        timeout-minutes: 5
        if: github.event_name == 'pull_request'
        env:
          # Through env, not interpolated into the script, so the expression
          # cannot become shell syntax.
          BASE_SHA: \${{ github.event.pull_request.base.sha }}
        run: |
          : "\${RUNNER_TEMP:?RUNNER_TEMP is not set; this step writes its intermediate files there}"
          mkdir -p .output/base/conf.d
          if ! git cat-file -e "$BASE_SHA" 2>/dev/null; then
            echo "::error::base commit $BASE_SHA is not in this clone, so there is nothing to compare against. Either the checkout was narrowed (this workflow sets fetch-depth: 0) or the base ref was rewritten."
            exit 1
          fi
          # ls-tree reads the ENTRY; git cat-file -t would resolve what it
          # points at, and for a submodule that object is absent here - which
          # would be misreported as "no config directory yet".
          git ls-tree "$BASE_SHA" -- conf.d > "$RUNNER_TEMP"/entry.txt
          kind=$(cut -d' ' -f2 "$RUNNER_TEMP"/entry.txt)
          if [ -z "$kind" ]; then kind=missing; fi
          if [ "$kind" = tree ]; then
            GIT_INDEX_FILE="$RUNNER_TEMP"/base.idx git read-tree "$BASE_SHA:conf.d"
            GIT_INDEX_FILE="$RUNNER_TEMP"/base.idx git checkout-index -a -f --prefix=.output/base/conf.d/
          elif [ "$kind" = missing ]; then
            echo "::notice::conf.d does not exist at $BASE_SHA; treating this as the first import, so every tenant is reported as added"
          else
            echo "::error::conf.d at $BASE_SHA is a $kind, not a directory, so no baseline can be built from it. Reporting that as a first import would hide the fault."
            exit 1
          fi
      - name: Config diff (blast radius)
        id: diff
        timeout-minutes: 5
        run: |
          # config-diff signals findings through its exit code, so both 0 (no
          # change) and 1 (changes) are ordinary outcomes here; 2 and above
          # mean the run did not complete.
          # ⚠️ It compares TENANT files only and skips every name starting
          # with _, so a change to conf.d/_defaults.yaml - inherited by every
          # tenant - reports "no changes". Review those by hand.
          set +e
          docker run --rm \\
            -v "\${{ github.workspace }}/.output/base/conf.d:/data/conf.d.base:ro" \\
            -v "\${{ github.workspace }}/conf.d:/data/conf.d:ro" \\
            ${image} \\
            config-diff --old-dir /data/conf.d.base --new-dir /data/conf.d \\
              --format markdown > .output/blast-radius.md
          rc=$?
          set -e
          if [ "$rc" -gt 1 ]; then
            echo "::error::config-diff exited $rc (expected 0 or 1) — image pull, mount, or malformed config"
            exit "$rc"
          fi
          if [ ! -s .output/blast-radius.md ]; then
            echo "::error::config-diff exited $rc but produced an empty report; treating this as a failed run rather than publishing it"
            exit 1
          fi
      - name: Explain a blast radius that could not be computed
        # The comment below is edited in place, so skipping it on a failure
        # leaves the PREVIOUS run's report standing as though it were current -
        # and stale is worse than absent, because stale looks like the answer.
        # !cancelled() rather than always(): a run the concurrency group
        # superseded must not overwrite the winner's comment.
        if: \${{ !cancelled() }}
        env:
          ROUTES_OUTCOME: \${{ steps.routes.outcome }}
          SNAPSHOT_OUTCOME: \${{ steps.snapshot.outcome }}
          DIFF_OUTCOME: \${{ steps.diff.outcome }}
        run: |
          if [ "$DIFF_OUTCOME" = success ] && [ -s .output/blast-radius.md ]; then
            exit 0
          fi
          mkdir -p .output
          {
            echo "## Blast radius: NOT COMPUTED"
            echo
            echo "This run could not compute the tenant-config blast radius, so this comment replaces the previous run's report. **Nothing here says the change is safe - it says nobody measured it.**"
            echo
            echo "| step | outcome |"
            echo "| --- | --- |"
            echo "| Generate Alertmanager routes | \\\`\${ROUTES_OUTCOME:-did not run}\\\` |"
            echo "| Resolve base config snapshot | \\\`\${SNAPSHOT_OUTCOME:-did not run}\\\` |"
            echo "| Config diff (blast radius) | \\\`\${DIFF_OUTCOME:-did not run}\\\` |"
            echo
            echo "Open the failing step in this run's log: each failure path prints an ::error:: line naming the cause."
          } > .output/blast-radius.md
      - name: Post PR comment with blast radius
        # Same condition as the step above, on purpose.
        if: \${{ !cancelled() }}
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          path: .output/blast-radius.md
          header: dynamic-alerting-blast-radius

  apply:
    needs: [validate]
    if: github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest
    environment: production
    # Wider than the other two: this one talks to a cluster.
    timeout-minutes: 30
    concurrency:
      # cancel-in-progress: FALSE - cancelling this interrupts kubectl apply or
      # helm upgrade partway and leaves the cluster in a state no commit
      # describes. The group still serialises two manual dispatches.
      # Keyed on the target namespace, NOT on the ref: this job is
      # workflow_dispatch-only, a dispatch can start from any branch, and every
      # one of them deploys to the same namespace - a ref-keyed group would put
      # two dispatches in different groups and let them apply at the same time.
      group: dynamic-alerting-apply-monitoring
      cancel-in-progress: false
    steps:${config.deploy === 'kustomize' ? `
      - uses: actions/checkout@v6
      - name: Build ConfigMaps via Kustomize
        run: |
          # --load-restrictor: conf.d files are symlinked into kustomize/base/,
          # and the default restrictor refuses a symlink pointing outside it.
          kustomize build --load-restrictor LoadRestrictionsNone "kustomize/overlays/prod" > /tmp/manifests.yaml
      - name: Apply to cluster (dry-run first)
        run: |
          kubectl apply --dry-run=server -f /tmp/manifests.yaml
          echo "--- Dry-run passed. Applying... ---"
          kubectl apply -f /tmp/manifests.yaml
      - name: Reload Prometheus
        run: |
          kubectl rollout restart deployment/prometheus -n monitoring` : config.deploy === 'helm' ? `
      - uses: actions/checkout@v6
      - name: Helm upgrade threshold-exporter
        run: |
          helm upgrade --install threshold-exporter \\
            oci://ghcr.io/vencil/charts/threshold-exporter \\
            -f "environments/prod/values.yaml" \\
            -n monitoring \\
            --wait --timeout 5m` : ''}`;
}

export { CICD_DEFAULT_DA_TOOLS_IMAGE, cicdDaToolsImage, cicdImageIsMutable, cicdSplitImageRef, cicdGenerateInitCommand, cicdGenerateDockerCommand, cicdGeneratedPaths, cicdGenerateFileTree, cicdGenerateGitHubActionsPreview };
