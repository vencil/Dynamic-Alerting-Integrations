import{a as f}from"./chunk-ZHLICTNA.js";import{a as b,b as T,c as P,d as _,e as g}from"./chunk-6CZKKW3I.js";var C=b(T(),1),G=b(P(),1);var h=b(T(),1);var c=window.__t||((e,a)=>a),v=[{id:"ci",label:()=>c("CI/CD \u5E73\u53F0","CI/CD Platform")},{id:"deploy",label:()=>c("\u90E8\u7F72\u65B9\u5F0F","Deployment Mode")},{id:"packs",label:()=>c("Rule Packs","Rule Packs")},{id:"tenants",label:()=>c("Tenant \u8A2D\u5B9A","Tenant Setup")},{id:"review",label:()=>c("\u6AA2\u8996\u8207\u7522\u51FA","Review & Generate")}],E=[{id:"mariadb",label:"MariaDB/MySQL",category:"database",icon:"\u{1F42C}",defaultOn:!0},{id:"postgresql",label:"PostgreSQL",category:"database",icon:"\u{1F418}"},{id:"redis",label:"Redis",category:"database",icon:"\u{1F534}"},{id:"mongodb",label:"MongoDB",category:"database",icon:"\u{1F343}"},{id:"elasticsearch",label:"Elasticsearch",category:"database",icon:"\u{1F50E}"},{id:"oracle",label:"Oracle",category:"database",icon:"\u{1F3DB}\uFE0F"},{id:"db2",label:"DB2",category:"database",icon:"\u{1F537}"},{id:"clickhouse",label:"ClickHouse",category:"database",icon:"\u{1F5B1}\uFE0F"},{id:"kafka",label:"Kafka",category:"messaging",icon:"\u{1F4E8}"},{id:"rabbitmq",label:"RabbitMQ",category:"messaging",icon:"\u{1F430}"},{id:"jvm",label:"JVM",category:"runtime",icon:"\u2615"},{id:"nginx",label:"Nginx",category:"webserver",icon:"\u{1F310}"},{id:"kubernetes",label:"Kubernetes",category:"infrastructure",icon:"\u2388",defaultOn:!0}],y=[{id:"github",label:"GitHub Actions",icon:"\u{1F419}",desc:()=>c("\u6700\u5EE3\u6CDB\u4F7F\u7528\u7684 CI/CD\uFF0C\u76F4\u63A5\u6574\u5408 GitHub","Most widely used CI/CD, built into GitHub.")},{id:"gitlab",label:"GitLab CI",icon:"\u{1F98A}",desc:()=>c("GitLab \u5167\u5EFA CI/CD\uFF0C\u652F\u63F4\u81EA\u8A17\u7BA1","GitLab built-in CI/CD, supports self-hosted.")},{id:"both",label:c("\u5169\u8005\u7686\u6709","Both"),icon:"\u{1F504}",desc:()=>c("\u540C\u6642\u7522\u751F GitHub Actions \u548C GitLab CI \u914D\u7F6E","Generate both GitHub Actions and GitLab CI configs.")}],w=[{id:"kustomize",label:"Kustomize",icon:"\u{1F4E6}",desc:()=>c("\u63A8\u85A6\u5165\u9580\uFF1AconfigMapGenerator \u81EA\u52D5\u7522\u751F ConfigMap","Recommended to start: configMapGenerator auto-creates ConfigMap.")},{id:"helm",label:"Helm",icon:"\u26F5",desc:()=>c("\u4F7F\u7528 threshold-exporter Helm chart \u7BA1\u7406","Managed via threshold-exporter Helm chart.")}];var m="ghcr.io/vencil/da-tools:latest";function x(e){let a=e&&e.daToolsImage,r=typeof a=="string"?a.trim():"";return r===""?m:r}function R(e){let a=e.indexOf("@");if(a!==-1)return{repo:e.slice(0,a),tag:null,digest:!0};let r=e.lastIndexOf("/"),n=e.slice(r+1),l=n.indexOf(":");return l===-1?{repo:e,tag:null,digest:!1}:{repo:e.slice(0,r+1)+n.slice(0,l),tag:n.slice(l+1),digest:!1}}var L=R(m).repo;function M(e){let{repo:a,tag:r,digest:n}=R(e);return n?!1:r===null||r==="latest"?!0:a!==L}function k(e){let a=["da-tools init"];e.ci&&a.push(`--ci ${e.ci}`),e.deploy&&a.push(`--deploy ${e.deploy}`),e.tenants.length>0&&a.push(`--tenants ${e.tenants.join(",")}`),e.packs.length>0&&a.push(`--rule-packs ${e.packs.join(",")}`);let r=x(e);return r!==m&&a.push(`--da-tools-image ${r}`),a.push("--non-interactive"),a.join(` \\
  `)}function A(e){let a=k(e);return`docker run --rm -it \\
  --user $(id -u):$(id -g) \\
  -v "$(pwd):/workspace" -w /workspace \\
  ${x(e)} \\
  ${a.replace("da-tools ","")}`}function j(e){let a=["conf.d/_defaults.yaml"];for(let r of e.tenants)a.push(`conf.d/${r}.yaml`);return(e.ci==="github"||e.ci==="both")&&a.push(".github/workflows/dynamic-alerting.yaml"),(e.ci==="gitlab"||e.ci==="both")&&(a.push(".gitlab-ci.d/dynamic-alerting.yml"),a.push(".gitlab-ci.yml")),e.deploy==="helm"&&a.push("environments/prod/values.yaml"),e.deploy==="kustomize"&&(a.push("kustomize/base/kustomization.yaml"),a.push("kustomize/base/README.md"),a.push("kustomize/overlays/dev/kustomization.yaml"),a.push("kustomize/overlays/prod/kustomization.yaml")),a.push(".pre-commit-config.da.yaml"),a.push(".da-init.yaml"),a}function S(e,a){let r=[],n=Object.keys(e);return n.forEach((l,i)=>{let s=i===n.length-1,d=e[l],u=d!==null;r.push(`${a}${s?"\u2514\u2500\u2500 ":"\u251C\u2500\u2500 "}${l}${u?"/":""}`),u&&r.push(...S(d,`${a}${s?"    ":"\u2502   "}`))}),r}function D(e){let a={};for(let r of j(e)){let n=r.split("/"),l=a;n.forEach((i,s)=>{s===n.length-1?l[i]=null:(l[i]=l[i]||{},l=l[i])})}return["your-repo/",...S(a,"")].join(`
`)}function F(e){let a=["conf.d"];return e.deploy==="kustomize"&&a.push("kustomize"),e.deploy==="helm"&&a.push("environments"),a.push("rule-packs"),a.sort()}var H={kustomize:{header:`  # \u2500\u2500 Stage 3: Apply (manual trigger only) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
`,steps:`      - name: Build ConfigMaps via Kustomize
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
`},helm:{header:`  # \u2500\u2500 Stage 3: Apply via Helm (manual trigger only) \u2500\u2500\u2500\u2500\u2500
`,steps:`      - name: Helm upgrade threshold-exporter
        run: |
          helm upgrade --install threshold-exporter \\
            oci://ghcr.io/vencil/charts/threshold-exporter \\
            -f "environments/prod/values.yaml" \\
            -n \${{ env.MONITORING_NS }} \\
            --wait --timeout 5m
`}};function O(e){if(e.deploy!=="kustomize"&&e.deploy!=="helm")throw new Error(`cicdGenerateGitHubActionsPreview: unknown deploy method ${JSON.stringify(e.deploy)} \u2014 expected 'kustomize' or 'helm'. A new deploy method needs an apply block here AND in scripts/tools/ops/init_project.py.`);let a=H,r=x(e),n=M(r)?`# ${r} can be repointed at different code without this file changing - :latest moves by design, any other tag at its registry's discretion. Pin a digest if this pipeline has to be reproducible.
`:"",l=F(e).map(i=>`      - '${i}/**'`).join(`
`);return`# Dynamic Alerting CI/CD Pipeline
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
${l}
  # \u26D4 No \`branches:\` filter, deliberately. \`on.push.branches\` takes literals
  # only \u2014 no expressions \u2014 so any value we write here is a guess about the
  # customer's default branch, and \`main\` is wrong for every \`master\` /
  # \`trunk\` / \`develop\` repo. Enumerating the common names is the same
  # denylist mistake one size larger. Omitting the filter is the only form
  # that is correct for everyone, and it can only ADD runs: a push to the
  # default branch is still a push, so the post-merge re-validation this leg
  # exists for is unchanged. The extra runs are \`validate\` alone (\`generate\`
  # is pull_request-only, \`apply\` is workflow_dispatch-only) \u2014 a read-only
  # \`docker run validate-config\` with no credentials, already narrowed by
  # the paths filter below. The GitLab leg gets portability from
  # \`$CI_DEFAULT_BRANCH\`; this is the GitHub equivalent.
  # \u26D4 The SAME trees as the pull_request leg \u2014 one rendered list
  # substituted twice, so the two cannot drift apart by editing one. It used
  # to list \`conf.d/**\` alone, so a direct push touching only
  # \`rule-packs/custom/**\` ran nothing \u2014 and the custom-rule governance lint
  # inside \`validate\` is scoped to exactly that tree. A repo that permits
  # direct pushes got no lint at all on tenant-authored PromQL pushed that
  # way.
  # \u26D4 WHICH trees depends on this invocation's \`--deploy\` and
  # \`--config-source\`, and both directions of getting that wrong are
  # silent (issue 1473): a filter naming a tree nothing here writes and no
  # job reads can never match, while a tree a job DOES read \u2014 helm's
  # \`environments/prod/values.yaml\` \u2014 that is absent from the filter means
  # editing it creates no job at all and the pull request goes green having
  # validated nothing.
  push:
    paths:
${l}
  workflow_dispatch:

# Least-privilege, and \`pull-requests: write\` is LOAD-BEARING, not
# boilerplate: the generate job's only output is a sticky PR comment, and
# GITHUB_TOKEN defaults to read-only on repositories created after 2023-02.
# Without this, Stage 2's comment step 403s and the customer's blast-radius
# review never appears. Same declaration this platform's own backtest.yaml
# carries for the same action.
#
# \u26D4 SCOPE \u2014 on a pull_request from a FORK this block is not enough on its
# own, and the reason is a repo SETTING rather than a hard platform lock.
# By default a fork PR's GITHUB_TOKEN is read-only no matter what
# \`permissions:\` asks for, so the comment step 403s. The documented
# exception is the admin toggle "Send write tokens to workflows from pull
# requests" \u2014 which lives under the settings for forks of PRIVATE
# repositories, i.e. exactly the shape most customers deploy this in. With
# that toggle on, this \`permissions:\` block is what grants the write, so it
# is load-bearing in that configuration too.
#
# We deliberately do NOT use \`pull_request_target\` / \`workflow_run\` to buy
# the elevated token, because both run trusted code against untrusted input.
# So: same-repo branches work as-is; fork PRs need the customer's admin to
# make that call knowingly. Stated here rather than silently implied either
# way \u2014 the previous wording claimed the platform hard-locks this and that
# a different design was required, which is not what the docs say.
#
# \u26D4 The write scope sits on the \`generate\` job, NOT here. At workflow level
# every job inherits it, including \`apply\` \u2014 the one carrying
# \`environment: production\` and cluster credentials, which posts no comment
# and needs no PR write. This is the shape this platform uses on itself in
# 6 workflows / 9 jobs (bench-on-demand.yaml's \`gate\` job is identical:
# \`contents: read\` at the top, \`pull-requests: write\` on the one job that
# comments).
permissions:
  contents: read

env:
  DA_TOOLS_IMAGE: ${r}
  CONFIG_DIR: conf.d
  MONITORING_NS: monitoring

${n}jobs:
  # \u2500\u2500 Stage 1: Validate \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
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
          # \u26D4 The same guard the config-diff step carries, and for the same
          # reason: \`docker -v\` CREATES a missing host path instead of
          # failing, so a wrong or moved CONFIG_DIR mounts an EMPTY
          # directory, \`validate-config\` parses zero files and exits 0.
          # Stage 2 refuses to compare in that case; Stage 1 went green and
          # silent \u2014 the worse half, because nothing on the PR hints at it.
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

  # \u2500\u2500 Stage 2: Generate routes + blast radius \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  generate:
    # \u26D4 NO \`needs:\` \u2014 deliberately, and this is a fix rather than an
    # omission. It used to be \`needs: validate\`, and the \`if:\` below carries
    # no status function, so an implicit \`success()\` applied: any red in
    # \`validate\` skipped this whole job. \`validate\` lints custom Prometheus
    # rules under the rule-packs tree, which has NO causal relationship to
    # the tenant-config blast radius \u2014 so one unrelated lint ERROR anywhere
    # in that tree took the blast-radius comment away from EVERY config pull
    # request in the repository until somebody fixed it, and what reviewers
    # saw meanwhile was the previous run's report, which looks current.
    # This platform's own config-diff.yaml computes its blast radius with no
    # \`needs:\` at all, for the same reason.
    # \u26A0\uFE0F This is not "validation no longer gates anything": \`apply\` still
    # needs \`validate\`, and that is the edge that protects the cluster.
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    # \u26D4 The OUTER bound, and it has to stay above the sum of the step caps
    # below (6+1+5+5+5+2+2 = 26). If the job cap fires first the run is
    # CANCELLED, and every \`!cancelled()\` step \u2014 the fallback report and the
    # comment \u2014 is skipped: the issue 1421 stale comment, reached through a
    # timeout instead of through a failing step. Which is also why EVERY
    # step below carries its own cap, including the cheap ones: an uncapped
    # step has no way to end except by taking the job cap with it. Held by
    # \`test_step_timeouts_leave_room_under_the_job_timeout\`, which grades
    # both halves \u2014 no uncapped step, and the sum under this number.
    timeout-minutes: 30
    concurrency:
      # \u26D4 Load-bearing for the COMMENT, not just for runner minutes. The
      # sticky comment is edited in place under a fixed header, so two runs
      # of this job race to overwrite the same comment body and the loser is
      # whichever finishes LAST \u2014 not whichever commit is newer. Push twice
      # quickly and the PR can end up showing the older commit's blast
      # radius, with nothing in the comment to say so.
      group: dynamic-alerting-blast-radius-\${{ github.event.pull_request.number || github.ref }}
      cancel-in-progress: true
    # The only job that writes anything back to the PR. A job-level block
    # REPLACES the workflow one rather than merging, so \`contents: read\` is
    # restated here \u2014 dropping it would 403 the checkout.
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v6
        # The one step here whose honest cap is not small \u2014 a full-history
        # clone of a large repository takes minutes. It is still capped:
        # see the job cap above for what an uncapped step costs.
        timeout-minutes: 6
        with:
          # Load-bearing. The blast radius is computed against the PR's
          # base commit, and checkout's default (fetch-depth: 1) does not
          # put that object in the clone \u2014 the lookup below then fails and
          # the report silently degrades to "every tenant is new", which
          # docs/internal/lint-policy.md names as pretending to be
          # diff-aware. Same value and same reason as this platform's own
          # config-diff.yaml and blast-radius.yml \u2014 but note those reach
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
        # \u26D4 Step caps, and the arithmetic is the point: every step in this
        # job is capped and the caps sum to 26, under this job's 30, so a hung
        # computation FAILS AS A STEP and the fallback below still runs. If
        # the JOB cap fired first the run would be CANCELLED, and both the
        # fallback and the comment step carry \`!cancelled()\` \u2014 correct for a
        # superseded run, but it would leave the previous report standing
        # with nothing to say the new run died. Held by
        # \`test_step_timeouts_leave_room_under_the_job_timeout\`.
        timeout-minutes: 5
        run: |
          # Read-only, deliberately: this step VALIDATES the routes it
          # would generate; nothing downstream consumes a written file.
          # An earlier template mounted \`.output\` writable and asked for
          # an output file together with \`--validate\` \u2014 but \`--validate\`
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
          # runner \u2014 it is a guard for anything replaying these steps
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
            echo "::error::base commit $BASE_SHA is not in this clone, so there is nothing to compare against. Two causes: the checkout was narrowed (this workflow sets fetch-depth: 0 \u2014 check it is still there), or the base ref was rewritten and that commit no longer exists, which is what a force-push, or re-running an old job whose recorded base commit is gone, looks like."
            exit 1
          fi
          # Read the entry from its PARENT tree rather than asking about
          # the object at that path. \`git cat-file -t "$BASE_SHA:$dir"\`
          # looks up the object the entry POINTS AT, and for a submodule
          # that is a commit belonging to another repository \u2014 absent
          # here, so it fails, and the fault would be filed as "no config
          # directory at the base", i.e. a first import. Measured: a
          # gitlink took the first-import branch and exited 0 with an
          # empty baseline. \`ls-tree\` reads the entry itself, so it can
          # say "commit" without ever resolving it.
          git ls-tree "$BASE_SHA" -- "$config_dir" > "$RUNNER_TEMP"/entry.txt
          kind=$(cut -d' ' -f2 "$RUNNER_TEMP"/entry.txt)
          if [ -z "$kind" ]; then kind=missing; fi
          if [ "$kind" = tree ]; then
            # \u26D4 Extracted through a scratch index, NOT through
            # \`git archive\`. \`export-ignore\` is an ARCHIVE-ONLY attribute
            # (git: "won't be added to archive files"), so a
            # .gitattributes rule covering the config directory makes
            # \`git archive\` emit a partial or empty baseline while every
            # command reports success \u2014 and the diff then overstates or
            # invents changes.
            #
            # Three separate attempts to DETECT that after the fact were
            # each falsified by measurement: a file count disagreed with
            # \`find\` over symlinks; a name set disagreed with the escaping
            # \`core.quotePath\` applies to non-ASCII, quote and backslash
            # paths; and "did anything arrive at all" was satisfied by a
            # stray README while all three tenant files were missing.
            # Reading the tree into an index and checking it out removes
            # the failure mode instead of watching for it \u2014 no comparison,
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
          # \`on.pull_request.paths\` above (not conf.d/ alone) \u2014 so both 0
          # and 1 are ordinary outcomes here.
          #   0 = no config change  -> the report says so in words, and the
          #       comment below is refreshed with it. Not skipped: this job
          #       also runs for edits to the other watched trees, which do
          #       not touch tenant config at all, and skipping would leave
          #       the PREVIOUS run's report standing as though it were
          #       still current.
          #       \u26A0\uFE0F READ THIS BEFORE TRUSTING A "no changes" COMMENT.
          #       config-diff compares TENANT files only \u2014 it skips every
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
          # "no changes" report \u2014 non-empty, so the empty-report guard
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
            echo "::error::config-diff exited $rc (expected 0 or 1) \u2014 image pull, mount, or malformed config"
            exit "$rc"
          fi
          # rc alone is not enough. Anything in front of the tool can exit
          # 1 with nothing on stdout \u2014 a mistyped or renamed subcommand
          # leaves the entrypoint exiting 1, which is indistinguishable
          # from "changes detected" by exit code alone, and the comment
          # comment action would then fail with "Either message or path
          # input is required" \u2014 an error pointing at an input that WAS
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
        # \u26D4 The point of this step is that the comment below has something
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
            echo "This run could not compute the tenant-config blast radius, so this comment replaces the previous run's report. **Nothing here says the change is safe \u2014 it says nobody measured it.**"
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

${a[e.deploy].header}  # \`needs: [validate]\` only \u2014 \`generate\` is pull_request-only, and a
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
      # \u26D4 \`cancel-in-progress: FALSE\`, and the difference from the other two
      # jobs is the whole reason this block is spelled out per job rather
      # than once at workflow level. Cancelling a read-only validate or a
      # superseded diff costs nothing; cancelling this one interrupts
      # \`kubectl apply\` / \`helm upgrade\` partway and leaves the cluster in a
      # state no commit describes.
      # \u26D4 The group is keyed on the TARGET NAMESPACE, not on the ref. This
      # job is workflow_dispatch-only and a dispatch can start from any
      # branch, while every one of them deploys to the same namespace \u2014 so a
      # ref-keyed group puts two dispatches in DIFFERENT groups and lets
      # them run \`kubectl apply\` / \`helm upgrade\` at the same time, which is
      # the exact interleaving this block exists to prevent. A job-level
      # \`concurrency\` cannot read \`env:\`, so the namespace is baked in here
      # at generation time.
      group: dynamic-alerting-apply-monitoring
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@v6
${a[e.deploy].steps}`}var t=b(_(),1),o=window.__t||((e,a)=>a);function q({config:e,onChange:a}){return(0,t.jsxs)("div",{children:[(0,t.jsx)("h3",{className:"text-lg font-semibold mb-2",children:o("\u9078\u64C7 CI/CD \u5E73\u53F0","Choose CI/CD Platform")}),(0,t.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mb-4",children:o("da-tools init \u6703\u70BA\u4F60\u7522\u751F\u5C0D\u61C9\u7684 pipeline \u914D\u7F6E\u6A94\u3002","da-tools init will generate the corresponding pipeline config files.")}),(0,t.jsx)("div",{className:"grid grid-cols-1 gap-3",role:"radiogroup","aria-label":o("CI/CD \u5E73\u53F0\u9078\u64C7","CI/CD platform selection"),children:y.map(r=>(0,t.jsx)("button",{onClick:()=>a({...e,ci:r.id}),role:"radio","aria-checked":e.ci===r.id,className:`p-4 rounded-lg border-2 text-left transition-all ${e.ci===r.id?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)]"}`,children:(0,t.jsxs)("div",{className:"flex items-center gap-3",children:[(0,t.jsx)("span",{className:"text-2xl","aria-hidden":"true",children:r.icon}),(0,t.jsxs)("div",{children:[(0,t.jsx)("div",{className:"font-medium text-[color:var(--da-color-fg)]",children:r.label}),(0,t.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)]",children:r.desc()})]})]})},r.id))})]})}function U({config:e,onChange:a}){return(0,t.jsxs)("div",{children:[(0,t.jsx)("h3",{className:"text-lg font-semibold mb-2",children:o("\u9078\u64C7\u90E8\u7F72\u65B9\u5F0F","Choose Deployment Mode")}),(0,t.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mb-4",children:o("\u5F9E YAML \u914D\u7F6E\u5230 Kubernetes ConfigMap \u7684\u90E8\u7F72\u8DEF\u5F91\u3002","The deployment path from YAML configs to Kubernetes ConfigMap.")}),(0,t.jsx)("div",{className:"grid grid-cols-1 gap-3",role:"radiogroup","aria-label":o("\u90E8\u7F72\u65B9\u5F0F\u9078\u64C7","Deployment mode selection"),children:w.map(r=>(0,t.jsx)("button",{onClick:()=>a({...e,deploy:r.id}),role:"radio","aria-checked":e.deploy===r.id,className:`p-4 rounded-lg border-2 text-left transition-all ${e.deploy===r.id?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)]"}`,children:(0,t.jsxs)("div",{className:"flex items-center gap-3",children:[(0,t.jsx)("span",{className:"text-2xl","aria-hidden":"true",children:r.icon}),(0,t.jsxs)("div",{children:[(0,t.jsx)("div",{className:"font-medium text-[color:var(--da-color-fg)]",children:r.label}),(0,t.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)]",children:r.desc()})]})]})},r.id))})]})}function z({config:e,onChange:a}){let r=(0,h.useMemo)(()=>{let i={};for(let s of E)i[s.category]||(i[s.category]=[]),i[s.category].push(s);return i},[]),n={database:()=>o("\u8CC7\u6599\u5EAB","Databases"),messaging:()=>o("\u8A0A\u606F\u4F47\u5217","Messaging"),runtime:()=>o("\u904B\u884C\u74B0\u5883","Runtime"),webserver:()=>o("\u7DB2\u9801\u4F3A\u670D\u5668","Web Servers"),infrastructure:()=>o("\u57FA\u790E\u8A2D\u65BD","Infrastructure")},l=i=>{let s=e.packs.includes(i)?e.packs.filter(d=>d!==i):[...e.packs,i];a({...e,packs:s})};return(0,t.jsxs)("div",{children:[(0,t.jsx)("h3",{className:"text-lg font-semibold mb-2",children:o("\u9078\u64C7 Rule Pack","Select Rule Packs")}),(0,t.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mb-4",children:o("\u9078\u64C7\u4F60\u9700\u8981\u76E3\u63A7\u7684\u6280\u8853\u3002operational \u548C platform pack \u81EA\u52D5\u555F\u7528\u3002","Select technologies to monitor. Operational and platform packs are auto-enabled.")}),(0,t.jsx)("div",{className:"space-y-4",children:Object.entries(r).map(([i,s])=>(0,t.jsxs)("div",{children:[(0,t.jsx)("div",{className:"text-xs font-medium text-[color:var(--da-color-muted)] uppercase tracking-wide mb-2",id:`pack-cat-${i}`,children:n[i]?n[i]():i}),(0,t.jsx)("div",{className:"grid grid-cols-2 gap-2",role:"group","aria-labelledby":`pack-cat-${i}`,children:s.map(d=>{let u=e.packs.includes(d.id);return(0,t.jsxs)("button",{onClick:()=>l(d.id),"aria-pressed":u,className:`p-3 rounded-lg border text-left text-sm transition-all ${u?"border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]":"border-[color:var(--da-color-surface-border)] hover:border-[color:var(--da-color-card-hover-border)]"}`,children:[(0,t.jsx)("span",{className:"mr-2","aria-hidden":"true",children:d.icon}),(0,t.jsx)("span",{className:u?"font-medium text-[color:var(--da-color-fg)]":"text-[color:var(--da-color-fg)]",children:d.label}),u&&(0,t.jsx)("span",{className:"ml-1 text-[color:var(--da-color-accent)]","aria-hidden":"true",children:"\u2713"})]},d.id)})})]},i))}),(0,t.jsx)("div",{className:"mt-3 p-2 bg-[color:var(--da-color-surface-hover)] rounded text-xs text-[color:var(--da-color-muted)]",role:"status","aria-live":"polite",children:o(`\u5DF2\u9078 ${e.packs.length} \u500B Rule Pack\uFF08+ operational\u3001platform \u81EA\u52D5\u555F\u7528 = ${e.packs.length+2} \u500B\uFF09`,`${e.packs.length} selected (+ operational, platform auto-enabled = ${e.packs.length+2} total)`)})]})}function B({config:e,onChange:a}){let[r,n]=(0,h.useState)(""),l=()=>{let s=r.trim().toLowerCase().replace(/[^a-z0-9-]/g,"-");s&&!e.tenants.includes(s)&&(a({...e,tenants:[...e.tenants,s]}),n(""))},i=s=>{a({...e,tenants:e.tenants.filter(d=>d!==s)})};return(0,t.jsxs)("div",{children:[(0,t.jsx)("h3",{className:"text-lg font-semibold mb-2",children:o("\u8A2D\u5B9A Tenant","Configure Tenants")}),(0,t.jsx)("p",{className:"text-sm text-[color:var(--da-color-muted)] mb-4",children:o("\u6BCF\u500B tenant \u4EE3\u8868\u4E00\u7D44\u7368\u7ACB\u7684\u95BE\u503C\u914D\u7F6E\u3002\u4F8B\u5982 prod-mariadb, staging-redis\u3002","Each tenant represents an independent threshold config set. e.g. prod-mariadb, staging-redis.")}),(0,t.jsxs)("div",{className:"flex gap-2 mb-4",children:[(0,t.jsx)("label",{htmlFor:"cicd-tenant-input",className:"sr-only",children:o("Tenant \u540D\u7A31","Tenant name")}),(0,t.jsx)("input",{id:"cicd-tenant-input",type:"text",value:r,onChange:s=>n(s.target.value),onKeyDown:s=>s.key==="Enter"&&l(),placeholder:o("\u8F38\u5165 tenant \u540D\u7A31...","Enter tenant name..."),"aria-label":o("Tenant \u540D\u7A31","Tenant name"),className:"flex-1 px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"}),(0,t.jsx)("button",{onClick:l,disabled:!r.trim(),className:"px-4 py-2 bg-[color:var(--da-color-accent)] text-white rounded-lg text-sm hover:bg-[color:var(--da-color-accent-hover)] disabled:opacity-50",children:o("\u65B0\u589E","Add")})]}),(0,t.jsxs)("div",{className:"flex flex-wrap gap-1.5 mb-4",role:"group","aria-label":o("\u5FEB\u901F\u65B0\u589E tenant","Quick add tenants"),children:[(0,t.jsx)("span",{className:"text-xs text-[color:var(--da-color-muted)] self-center",children:o("\u5FEB\u901F\u65B0\u589E\uFF1A","Quick add:")}),["prod-mariadb","prod-redis","staging-app","dev-testing","prod-kafka"].map(s=>(0,t.jsxs)("button",{onClick:()=>{e.tenants.includes(s)||a({...e,tenants:[...e.tenants,s]})},disabled:e.tenants.includes(s),"aria-label":o(`\u5FEB\u901F\u65B0\u589E ${s}`,`Quick add ${s}`),className:"text-xs px-2 py-1 bg-[color:var(--da-color-tag-bg)] hover:bg-[color:var(--da-color-surface-hover)] rounded disabled:opacity-30",children:["+ ",s]},s))]}),e.tenants.length>0&&(0,t.jsx)("div",{className:"space-y-1",role:"list","aria-label":o("\u5DF2\u65B0\u589E\u7684 tenant","Added tenants"),children:e.tenants.map(s=>(0,t.jsxs)("div",{role:"listitem",className:"flex items-center justify-between p-2 bg-[color:var(--da-color-surface-hover)] rounded",children:[(0,t.jsx)("code",{className:"text-sm font-mono text-[color:var(--da-color-fg)]",children:s}),(0,t.jsx)("button",{onClick:()=>i(s),"aria-label":o(`\u79FB\u9664 tenant ${s}`,`Remove tenant ${s}`),className:"text-xs font-semibold text-[color:var(--da-color-error)] hover:opacity-80",children:o("\u79FB\u9664","Remove")})]},s))}),e.tenants.length===0&&(0,t.jsx)("div",{className:"text-sm text-[color:var(--da-color-muted)] text-center py-4",role:"status","aria-live":"polite",children:o("\u5C1A\u672A\u65B0\u589E tenant","No tenants added yet")})]})}function V({config:e,onChange:a}){let r=f(),n=f(),[l,i]=(0,h.useState)(!1),s=k(e),d=A(e),u=D(e),I=e.ci&&e.deploy&&e.packs.length>0&&e.tenants.length>0;return(0,t.jsxs)("div",{children:[(0,t.jsx)("h3",{className:"text-lg font-semibold mb-2",children:o("\u6AA2\u8996\u914D\u7F6E","Review Configuration")}),!I&&(0,t.jsx)("div",{className:"p-3 bg-[color:var(--da-color-warning-soft)] border border-[color:var(--da-color-warning)]/30 rounded-lg text-sm text-[color:var(--da-color-warning)] mb-4",role:"alert","aria-live":"polite",children:o("\u8ACB\u5B8C\u6210\u6240\u6709\u6B65\u9A5F\u5F8C\u518D\u7522\u751F\u547D\u4EE4\u3002","Please complete all steps before generating commands.")}),(0,t.jsxs)("div",{className:"grid grid-cols-2 gap-3 mb-4",role:"status","aria-live":"polite","aria-atomic":"true","aria-label":o("\u914D\u7F6E\u6458\u8981","Configuration summary"),children:[(0,t.jsxs)("div",{className:"p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg",children:[(0,t.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)]",children:o("CI/CD \u5E73\u53F0","CI/CD Platform")}),(0,t.jsx)("div",{className:"font-medium text-sm text-[color:var(--da-color-fg)]",children:y.find(p=>p.id===e.ci)?.label||"-"})]}),(0,t.jsxs)("div",{className:"p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg",children:[(0,t.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)]",children:o("\u90E8\u7F72\u65B9\u5F0F","Deployment")}),(0,t.jsx)("div",{className:"font-medium text-sm text-[color:var(--da-color-fg)]",children:w.find(p=>p.id===e.deploy)?.label||"-"})]}),(0,t.jsxs)("div",{className:"p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg",children:[(0,t.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)]",children:"Rule Packs"}),(0,t.jsxs)("div",{className:"font-medium text-sm text-[color:var(--da-color-fg)]",children:[e.packs.length," + 2 ",o("\u81EA\u52D5","auto")]})]}),(0,t.jsxs)("div",{className:"p-3 bg-[color:var(--da-color-surface-hover)] rounded-lg",children:[(0,t.jsx)("div",{className:"text-xs text-[color:var(--da-color-muted)]",children:"Tenants"}),(0,t.jsx)("div",{className:"font-medium text-sm text-[color:var(--da-color-fg)]",children:e.tenants.length})]})]}),I&&(0,t.jsxs)(t.Fragment,{children:[(0,t.jsxs)("div",{className:"mb-4",children:[(0,t.jsx)("label",{htmlFor:"cicd-da-tools-image",className:"block text-sm font-medium text-[color:var(--da-color-fg)] mb-1",children:o("da-tools \u6620\u50CF","da-tools image")}),(0,t.jsx)("input",{id:"cicd-da-tools-image",type:"text",value:e.daToolsImage??"",onChange:p=>a({...e,daToolsImage:p.target.value}),placeholder:m,"aria-label":o("da-tools \u6620\u50CF","da-tools image"),"aria-describedby":"cicd-da-tools-image-help",className:"w-full px-3 py-2 border border-[color:var(--da-color-surface-border)] rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]"}),(0,t.jsx)("p",{id:"cicd-da-tools-image-help",className:"mt-1 text-xs text-[color:var(--da-color-muted)]",children:o("\u7559\u7A7A\u5247\u7528\u9810\u8A2D\u3002\u7B49\u540C CLI \u7684 --da-tools-image\u3002:latest \u6211\u5011\u6703\u79FB\u52D5\uFF1B\u63DB\u6210\u4F60\u81EA\u5DF1\u7684 registry \u6642\uFF0Ctag \u4E00\u6A23\u53EF\u80FD\u88AB\u91CD\u6307\u2014\u2014\u8981\u53EF\u91CD\u73FE\u8ACB\u91D8 digest\u3002","Blank uses the default. Same knob as the CLI --da-tools-image. We move :latest; a tag on your own registry can be repointed too \u2014 pin a digest if you need reproducibility.")})]}),(0,t.jsxs)("div",{className:"mb-4",children:[(0,t.jsxs)("div",{className:"flex items-center justify-between mb-1",children:[(0,t.jsxs)("span",{className:"text-sm font-medium text-[color:var(--da-color-fg)]",children:["da-tools init ",o("\u547D\u4EE4","command")]}),(0,t.jsx)("button",{onClick:()=>r.copy(s),"aria-label":o("\u8907\u88FD da-tools init \u547D\u4EE4","Copy da-tools init command"),className:`text-xs px-2 py-1 rounded ${r.copied?"bg-[color:var(--da-color-success)] text-white":"bg-[color:var(--da-color-tag-bg)] hover:bg-[color:var(--da-color-surface-hover)]"}`,children:r.copied?(0,t.jsx)("span",{"aria-hidden":"true",children:"\u2713"}):o("\u8907\u88FD","Copy")})]}),(0,t.jsx)("pre",{className:"bg-[color:var(--da-color-hero-bg)] text-[color:var(--da-color-success)] p-4 rounded-lg text-xs font-mono overflow-x-auto",children:s})]}),(0,t.jsxs)("div",{className:"mb-4",children:[(0,t.jsxs)("div",{className:"flex items-center justify-between mb-1",children:[(0,t.jsx)("span",{className:"text-sm font-medium text-[color:var(--da-color-fg)]",children:o("Docker \u4E00\u9375\u57F7\u884C","Docker one-liner")}),(0,t.jsx)("button",{onClick:()=>n.copy(d),"aria-label":o("\u8907\u88FD Docker \u547D\u4EE4","Copy Docker command"),className:`text-xs px-2 py-1 rounded ${n.copied?"bg-[color:var(--da-color-success)] text-white":"bg-[color:var(--da-color-tag-bg)] hover:bg-[color:var(--da-color-surface-hover)]"}`,children:n.copied?(0,t.jsx)("span",{"aria-hidden":"true",children:"\u2713"}):o("\u8907\u88FD","Copy")})]}),(0,t.jsx)("pre",{className:"bg-[color:var(--da-color-hero-bg)] text-[color:var(--da-color-hero-accent)] p-4 rounded-lg text-xs font-mono overflow-x-auto",children:d})]}),(0,t.jsxs)("div",{className:"mb-4",children:[(0,t.jsx)("span",{className:"text-sm font-medium text-[color:var(--da-color-fg)]",children:o("\u7522\u751F\u7684\u6A94\u6848\u7D50\u69CB","Generated file structure")}),(0,t.jsx)("pre",{className:"mt-1 bg-[color:var(--da-color-surface-hover)] p-4 rounded-lg text-xs font-mono border border-[color:var(--da-color-surface-border)] text-[color:var(--da-color-fg)]",children:u})]}),(0,t.jsx)("button",{onClick:()=>i(!l),"aria-expanded":l,className:"text-sm text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] mb-2",children:l?o("\u25BE \u6536\u8D77 Pipeline \u9810\u89BD","\u25BE Hide pipeline preview"):o("\u25B8 \u5C55\u958B Pipeline \u9810\u89BD","\u25B8 Show pipeline preview")}),l&&(e.ci==="github"||e.ci==="both")&&(0,t.jsxs)("div",{className:"mb-4",children:[(0,t.jsx)("span",{className:"text-xs font-medium text-[color:var(--da-color-muted)]",children:".github/workflows/dynamic-alerting.yaml"}),(0,t.jsx)("pre",{className:"mt-1 bg-[color:var(--da-color-hero-bg)] text-[color:var(--da-color-hero-fg)] p-4 rounded-lg text-xs font-mono overflow-x-auto max-h-64 overflow-y-auto",children:O(e)})]}),(0,t.jsxs)("div",{className:"mt-4 p-4 bg-[color:var(--da-color-info-soft)] rounded-lg border border-[color:var(--da-color-info)]/30",children:[(0,t.jsx)("h4",{className:"text-sm font-medium text-[color:var(--da-color-accent-hover)] mb-2",children:o("\u4E0B\u4E00\u6B65","Next Steps")}),(0,t.jsxs)("ol",{className:"text-sm text-[color:var(--da-color-fg)] space-y-1 list-decimal list-inside",children:[(0,t.jsx)("li",{children:o("\u5728\u4F60\u7684 repo \u6839\u76EE\u9304\u57F7\u884C\u4E0A\u65B9\u7684 Docker \u547D\u4EE4","Run the Docker command above in your repo root")}),(0,t.jsx)("li",{children:o("\u7DE8\u8F2F conf.d/ \u4E2D\u7684 tenant YAML\uFF0C\u8ABF\u6574\u95BE\u503C","Edit tenant YAML in conf.d/, adjust thresholds")}),(e.ci==="gitlab"||e.ci==="both")&&(0,t.jsxs)("li",{children:[o("GitLab\uFF1A\u6839\u76EE\u9304 .gitlab-ci.yml \u662F GitLab \u552F\u4E00\u6703\u81EA\u52D5\u8F09\u5165\u7684\u8DEF\u5F91\u3002\u4F60\u7684 repo \u6C92\u6709\u9019\u500B\u6A94\u6848\u6642\uFF0Cinit \u6703\u7522\u751F\u4E00\u4EFD\uFF0C\u6838\u5FC3\u5C31\u662F\u4E0B\u9762\u9019\u500B include \u5340\u584A\u3002","GitLab: the repo-root .gitlab-ci.yml is the only path GitLab auto-loads. If your repo does not have one, init generates it, and the include block below is its working part."),(0,t.jsx)("br",{}),o("\u82E5\u4F60\u7684 repo \u5DF2\u7D93\u6709\u6839\u76EE\u9304 .gitlab-ci.yml\uFF1Ainit \u4E0D\u6703\u6539\u52D5\u5B83\uFF0C\u9700\u8981\u4F60\u81EA\u5DF1\u63A5\u3002\u6B64\u6642\u8981\u770B\u5B83\u6709\u6C92\u6709 include: \u9019\u500B\u9375\u2014\u2014\u6C92\u6709\u7684\u8A71\uFF0C\u628A\u4E0B\u9762\u6574\u584A\u52A0\u5728\u6A94\u6848\u6700\u5F8C\uFF1B\u5DF2\u7D93\u6709\u7684\u8A71\uFF0C\u53EA\u628A - local: \u90A3\u4E00\u9805\u52A0\u9032\u4F60\u65E2\u6709\u7684 include: \u5E95\u4E0B\uFF0C\u4E0D\u8981\u518D\u958B\u7B2C\u4E8C\u500B include: \u9375\uFF08\u5F8C\u8005\u6703\u84CB\u6389\u524D\u8005\uFF0C\u4F60\u539F\u672C\u7684 template \u6703\u6D88\u5931\uFF09\u3002\u5BE6\u969B\u8DD1 init \u6642\uFF0C\u5B83\u6703\u5224\u65B7\u4F60\u7684\u6A94\u6848\u5C6C\u65BC\u54EA\u4E00\u7A2E\u4E26\u5370\u51FA\u5C0D\u61C9\u7684\u505A\u6CD5\u3002","If your repo already has a root .gitlab-ci.yml: init leaves it untouched and you wire it yourself. Check whether it already has an include: key \u2014 if not, append the whole block below at the end of the file; if it does, add only the - local: item under your existing include:, and do NOT introduce a second include: key (the later one replaces the earlier, so your existing templates would vanish). Running init prints which of these cases your file is in."),(0,t.jsx)("pre",{className:"mt-1 bg-[color:var(--da-color-surface-hover)] p-4 rounded-lg text-xs font-mono border border-[color:var(--da-color-surface-border)] text-[color:var(--da-color-fg)]",children:`include:
  - local: .gitlab-ci.d/dynamic-alerting.yml`})]}),(0,t.jsx)("li",{children:e.ci==="gitlab"?o("git commit \u2192 GitLab CI \u57F7\u884C Validate\uFF08\u524D\u63D0\u662F\u4E0A\u4E00\u6B65\u7684 include \u5DF2\u5C31\u4F4D\uFF09\u3002GitLab \u9019\u4E00\u4EFD\u6C92\u6709 Generate \u968E\u6BB5\u2014\u2014\u6620\u50CF\u5167\u6C92\u6709 git\uFF0Cblast radius \u57FA\u6E96\u53D6\u4E0D\u5230\uFF0C\u898B issue 1358","git commit \u2192 GitLab CI runs Validate (once the include above is in place). The GitLab artifact has no Generate stage \u2014 its image has no git, so the blast-radius baseline cannot be taken; see issue 1358"):e.ci==="both"?o("git commit \u2192 GitHub Actions \u81EA\u52D5\u57F7\u884C Validate + Generate\uFF1BGitLab CI \u5728 include \u5C31\u4F4D\u5F8C\u53EA\u57F7\u884C Validate\uFF08\u6C92\u6709 Generate\uFF0C\u898B issue 1358\uFF09","git commit \u2192 GitHub Actions auto-runs Validate + Generate; GitLab CI runs Validate only once the include is in place (no Generate \u2014 see issue 1358)"):o("git commit \u2192 GitHub Actions \u81EA\u52D5\u57F7\u884C Validate + Generate","git commit \u2192 GitHub Actions auto-runs Validate + Generate")}),(0,t.jsx)("li",{children:o("PR \u5BE9\u6838\u901A\u904E\u5F8C\u624B\u52D5\u89F8\u767C Apply","After PR approval, manually trigger Apply")})]})]})]})]})}function N(){let[e,a]=(0,h.useState)(0),[r,n]=(0,h.useState)({ci:"github",deploy:"kustomize",packs:["mariadb","kubernetes"],tenants:[],daToolsImage:m}),l=(0,h.useMemo)(()=>{switch(e){case 0:return!!r.ci;case 1:return!!r.deploy;case 2:return r.packs.length>0;case 3:return r.tenants.length>0;default:return!1}},[e,r]);return(0,t.jsxs)("div",{className:"max-w-3xl mx-auto",children:[(0,t.jsxs)("div",{className:"mb-6",children:[(0,t.jsx)("h1",{className:"text-2xl font-bold text-[color:var(--da-color-fg)]",children:o("CI/CD \u5C0E\u5165\u7CBE\u9748","CI/CD Setup Wizard")}),(0,t.jsx)("p",{className:"text-[color:var(--da-color-muted)] mt-1",children:o("\u56DB\u6B65\u7522\u51FA\u5B8C\u6574\u7684 da-tools init \u547D\u4EE4\u548C CI/CD \u914D\u7F6E \u2014 \u5F9E\u96F6\u5230\u90E8\u7F72\u3002","Four steps to generate your complete da-tools init command and CI/CD config \u2014 from zero to deployment.")})]}),(0,t.jsx)("div",{className:"flex items-center mb-6 bg-[color:var(--da-color-surface-hover)] rounded-lg p-2",role:"list","aria-label":o("CI/CD \u8A2D\u5B9A\u6B65\u9A5F","CI/CD setup steps"),children:v.map((i,s)=>(0,t.jsxs)(h.default.Fragment,{children:[s>0&&(0,t.jsx)("div",{className:`flex-1 h-0.5 ${s<=e?"bg-[color:var(--da-color-accent)]":"bg-[color:var(--da-color-surface-border)]"}`,"aria-hidden":"true"}),(0,t.jsxs)("button",{onClick:()=>a(s),role:"listitem","aria-current":s===e?"step":void 0,className:`flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors ${s===e?"bg-[color:var(--da-color-accent)] text-white":s<e?"text-[color:var(--da-color-accent)] hover:bg-[color:var(--da-color-accent-soft)]":"text-[color:var(--da-color-muted)]"}`,children:[(0,t.jsx)("span",{className:`w-5 h-5 rounded-full flex items-center justify-center text-xs ${s<e?"bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)]":s===e?"bg-[color:var(--da-color-surface)] text-[color:var(--da-color-accent)]":"bg-[color:var(--da-color-tag-bg)] text-[color:var(--da-color-tag-fg)]"}`,"aria-hidden":"true",children:s<e?(0,t.jsx)("span",{"aria-hidden":"true",children:"\u2713"}):s+1}),(0,t.jsx)("span",{className:"hidden sm:inline",children:i.label()})]})]},i.id))}),(0,t.jsx)("div",{className:"bg-[color:var(--da-color-surface)] rounded-lg border border-[color:var(--da-color-surface-border)] p-6 mb-4",role:"region","aria-label":o(`\u6B65\u9A5F ${e+1} \u5167\u5BB9`,`Step ${e+1} content`),children:(0,t.jsxs)(g,{scope:"cicd-setup-wizard/step/"+e,children:[e===0&&(0,t.jsx)(q,{config:r,onChange:n}),e===1&&(0,t.jsx)(U,{config:r,onChange:n}),e===2&&(0,t.jsx)(z,{config:r,onChange:n}),e===3&&(0,t.jsx)(B,{config:r,onChange:n}),e===4&&(0,t.jsx)(V,{config:r,onChange:n})]},e)}),(0,t.jsxs)("div",{className:"flex justify-between",children:[(0,t.jsx)("button",{onClick:()=>a(Math.max(0,e-1)),disabled:e===0,className:"px-4 py-2 text-sm font-medium text-[color:var(--da-color-fg)] bg-[color:var(--da-color-tag-bg)] rounded-lg hover:bg-[color:var(--da-color-surface-hover)] disabled:opacity-30",children:o("\u4E0A\u4E00\u6B65","Back")}),e<v.length-1?(0,t.jsx)("button",{onClick:()=>a(e+1),disabled:!l,className:"px-4 py-2 text-sm font-medium text-white bg-[color:var(--da-color-accent)] rounded-lg hover:bg-[color:var(--da-color-accent-hover)] disabled:opacity-50",children:o("\u4E0B\u4E00\u6B65","Next")}):(0,t.jsx)("div",{className:"text-sm text-[color:var(--da-color-muted)]",children:o("\u8907\u88FD\u4E0A\u65B9\u547D\u4EE4\u958B\u59CB\u57F7\u884C","Copy the command above to get started")})]})]})}var $=document.getElementById("root");$&&(0,G.createRoot)($).render(C.default.createElement(g,{scope:"cicd-setup-wizard"},C.default.createElement(N)));
//# sourceMappingURL=cicd-setup-wizard.js.map
