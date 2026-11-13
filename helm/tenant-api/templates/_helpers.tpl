{{/*
Expand the name of the chart.
*/}}
{{- define "tenant-api.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "tenant-api.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "tenant-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: dynamic-alerting
{{- end }}

{{/*
Selector labels
*/}}
{{- define "tenant-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tenant-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Namespace the federation token-store ConfigMap lives in (#1313).

Empty `federation.store.namespace` = this release's namespace, which is the
historical behaviour and stays the default so existing installs are untouched.

⛔ SINGLE SOURCE. configmap-federation-store.yaml, the RBAC pair and the
TA_FEDERATION_NAMESPACE env all resolve the namespace THROUGH THIS TEMPLATE and
must never re-derive it. Three independent copies is exactly how the store and
its consumers drifted apart in the first place: a ConfigMap is namespace-scoped
and both the gateway and the reconciler consume it as a VOLUME, so they can only
ever resolve one in their OWN namespace — mounting is not an API read that can
cross namespaces.
*/}}
{{- define "tenant-api.federationStoreNamespace" -}}
{{- .Values.federation.store.namespace | default .Release.Namespace -}}
{{- end }}

{{/*
conf-dir volume definition — emptyDir or a PVC (the chart's own, or
confDir.pvc.existingClaim). The legal types are the branches here; the schema's
confDir.type enum must equal them plus the "hostPath" tombstone
(tests/helm/test_tenant_api_confdir.py).

⛔ hostPath was removed (#1018): it was the only chart option that fails even
Pod Security `baseline`, and with this chart's defaults it could not run at all
(fsGroup does not apply to hostPath, and git refuses a repository another uid
owns). "hostPath" stays in the schema enum, and confDir.hostPath stays as a key,
only so the old values reach this fail with the migration steps instead of a
bare schema error — and so a values file started from `helm show values`
(which carried `hostPath: ""`) still renders.
*/}}
{{- define "tenant-api.confDirGuard" -}}
{{- if or (eq .Values.confDir.type "hostPath") .Values.confDir.hostPath -}}
{{- fail "confDir.type=hostPath (and confDir.hostPath) were removed (#1018): a hostPath volume fails Pod Security baseline, and fsGroup does not apply to it. Put the repository on a PersistentVolume instead — e.g. a local or hostPath PV on the node, chown'd to podSecurityContext.runAsUser (65534 by default) — bind a PVC to it, then set confDir.type=pvc and confDir.pvc.existingClaim=<that PVC>; or set gitRepoUrl and let the chart clone. Remove confDir.hostPath from your values." -}}
{{- end -}}
{{- if and .Values.confDir.pvc.existingClaim (ne .Values.confDir.type "pvc") -}}
{{- fail (printf "confDir.pvc.existingClaim=%s is only read with confDir.type=pvc (got %q): without it the claim is ignored and conf.d is an emptyDir. Set confDir.type=pvc, or clear existingClaim." .Values.confDir.pvc.existingClaim .Values.confDir.type) -}}
{{- end -}}
{{- end }}

{{- define "tenant-api.confDirVolume" -}}
{{- include "tenant-api.confDirGuard" . -}}
{{- if eq .Values.confDir.type "pvc" }}
persistentVolumeClaim:
  claimName: {{ .Values.confDir.pvc.existingClaim | default (printf "%s-conf" (include "tenant-api.name" .)) }}
{{- else }}
emptyDir: {}
{{- end }}
{{- end }}

{{/*
Write-back mode guard (#2855). Renders nothing; fails the render when
writeBack is unset or incomplete. The chart used to pass no --write-mode at
all, so every chart install ran `direct`: commits that stay in this pod's
/conf.d, are never pushed, never reach threshold-exporter (it reads the
threshold-config ConfigMap built from the config repo) and, with the default
emptyDir, are lost on restart.
*/}}
{{- define "tenant-api.writeBackGuard" -}}
{{- $wb := .Values.writeBack -}}
{{- $mode := $wb.mode | default "" -}}
{{- if not (has $mode (list "pr-github" "pr-gitlab" "direct")) -}}
{{- fail (printf "writeBack.mode must be pr-github, pr-gitlab or direct (got %q). pr-github / pr-gitlab open a PR/MR per write, which your GitOps pipeline delivers to threshold-exporter after merge; direct keeps writes in this pod only (see values.yaml writeBack, #2855)." $mode) -}}
{{- end -}}
{{- if eq $mode "direct" -}}
{{- /* The boolean true only: a quoted "false" is a non-empty string, which
     a bare `if` would take as an acknowledgement. */ -}}
{{- if not (and (kindIs "bool" $wb.directAcknowledgeNotDelivered) $wb.directAcknowledgeNotDelivered) -}}
{{- fail "writeBack.mode=direct keeps every write in this pod's /conf.d: it is never pushed, never reaches threshold-exporter, and is lost on restart with the default emptyDir confDir. Use pr-github or pr-gitlab, or set writeBack.directAcknowledgeNotDelivered=true (the boolean, not a string) for a throwaway trial install (#2855)." -}}
{{- end -}}
{{- else -}}
{{- if not .Values.gitRepoUrl -}}
{{- fail (printf "writeBack.mode=%s needs gitRepoUrl: the PRs are pushed to that repository's origin (#2855)." $mode) -}}
{{- end -}}
{{- /* Before the https check, whose message echoes the URL: a password in
     it must reach neither an error nor the GIT_REPO_URL ConfigMap. Only a
     scheme://… URL: urlParse errors on scp-style git@host:path, which the
     https check below refuses anyway. */ -}}
{{- if and (contains "://" .Values.gitRepoUrl) (urlParse .Values.gitRepoUrl).userinfo -}}
{{- fail (printf "writeBack.mode=%s: gitRepoUrl must not carry credentials (user:password@); put the forge token in the writeBack.token Secret instead (#2855)." $mode) -}}
{{- end -}}
{{- if or (not (hasPrefix "https://" .Values.gitRepoUrl)) (not (urlParse .Values.gitRepoUrl).host) -}}
{{- fail (printf "writeBack.mode=%s needs an https:// gitRepoUrl (got %q): the forge token authenticates clone and push over HTTPS only, and is never sent over plain http (#2855)." $mode .Values.gitRepoUrl) -}}
{{- end -}}
{{- if not $wb.token.secretName -}}
{{- fail (printf "writeBack.mode=%s needs writeBack.token.secretName: an existing Secret holding the forge token (#2855)." $mode) -}}
{{- end -}}
{{- if not $wb.token.secretKey -}}
{{- fail (printf "writeBack.mode=%s needs writeBack.token.secretKey: the key of the token in that Secret (#2855)." $mode) -}}
{{- end -}}
{{- if and (eq $mode "pr-github") (not $wb.github.repo) -}}
{{- fail "writeBack.mode=pr-github needs writeBack.github.repo (owner/repo) (#2855)." -}}
{{- end -}}
{{- if and (eq $mode "pr-gitlab") (not $wb.gitlab.project) -}}
{{- fail "writeBack.mode=pr-gitlab needs writeBack.gitlab.project (group/project or numeric id) (#2855)." -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Git HTTPS credentials for the PR modes (#2855), shared by the clone init
container and tenant-api. A credential helper set through git's environment
config (GIT_CONFIG_COUNT, git >= 2.31) answers `get` from GIT_FORGE_USER /
GIT_FORGE_TOKEN, so the token never lands in the remote URL, .git/config,
the ConfigMap or a log line. It is scoped to gitRepoUrl's https host
(credential.https://<host>.helper): any other host gets no answer.
Empty in direct mode.
*/}}
{{- define "tenant-api.gitCredentialEnv" -}}
{{- $mode := .Values.writeBack.mode -}}
{{- if has $mode (list "pr-github" "pr-gitlab") }}
- name: GIT_FORGE_USER
  value: {{ ternary "x-access-token" "oauth2" (eq $mode "pr-github") | quote }}
- name: GIT_FORGE_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.writeBack.token.secretName }}
      key: {{ .Values.writeBack.token.secretKey }}
- name: GIT_TERMINAL_PROMPT
  value: "0"
- name: GIT_CONFIG_COUNT
  value: "1"
- name: GIT_CONFIG_KEY_0
  value: {{ printf "credential.https://%s.helper" (urlParse .Values.gitRepoUrl).host | quote }}
- name: GIT_CONFIG_VALUE_0
  value: '!f() { test "$1" = get || exit 0; echo "username=$GIT_FORGE_USER"; echo "password=$GIT_FORGE_TOKEN"; }; f'
{{- end }}
{{- end }}

{{/*
A numeric value rendered as a decimal integer string for a TA_* env var (#2862).
Helm decodes a whole number from a values file as float64, and `quote` prints a
float64 of 1e6 or more in exponent form ("1.048576e+06"); the binary's integer
parse rejects that, warns once and falls back to its default, so the configured
value is silently ignored. Numbers go through int64 first; a fractional number
fails the render, as does one outside the int64 range (#2927; int64 would wrap
it to an unrelated integer the binary accepts). A string is passed through unchanged, so a malformed string
still reaches the binary's own warn-and-default path rather than becoming 0
here (for TA_WRITE_QUEUE_DEPTH, 0 means shed every queued write).
Call with (list "<values path for the error>" <value>).
*/}}
{{- define "tenant-api.intEnv" -}}
{{- $path := index . 0 -}}
{{- $v := index . 1 -}}
{{- if or (kindIs "float64" $v) (kindIs "int" $v) (kindIs "int64" $v) -}}
{{- if ne (float64 $v) (floor (float64 $v)) -}}
{{- fail (printf "%s must be a whole number, got %v" $path $v) -}}
{{- end -}}
{{- if ne (float64 (int64 $v)) (float64 $v) -}}
{{- fail (printf "%s is out of range for an integer, got %v" $path $v) -}}
{{- end -}}
{{- int64 $v | toString | quote -}}
{{- else -}}
{{- $v | quote -}}
{{- end -}}
{{- end }}

{{/*
A TA_* env entry of the tenant-api container (ADR-036 §12): `- name: <NAME>`
and `value: "<v>"`. Arguments: (list "<NAME>" <value> "<values path>").

tenant-api reads an EMPTY TA_* variable as unset and silently uses its
default (envOrDefault), where an empty command-line flag was an explicit
value. So an empty or nil value fails the render here, naming the values
path; a setting whose absence is meant (`with` / `if` around the entry) keeps
that condition, only the value it renders may not be empty.
tests/helm/test_tenant_api_settings_are_env.py requires every TA_* entry of
deployment.yaml to be a literal constant, this helper or tenant-api.intEnv.
*/}}
{{- define "tenant-api.taEnv" -}}
{{- $name := index . 0 -}}
{{- $v := index . 1 -}}
{{- $path := index . 2 -}}
{{- if or (kindIs "invalid" $v) (eq (toString $v) "") -}}
{{- fail (printf "%s must not be empty: tenant-api reads an empty %s as unset and silently uses its default" $path $name) -}}
{{- end -}}
- name: {{ $name }}
  value: {{ toString $v | quote }}
{{- end }}

{{/*
Generator container (ADR-036 §3, #2902): fixed in-pod paths. Defined once
here; deployment.yaml and the chart tests read them from the render, never
from a second copy.

  socketDir   the emptyDir holding generator.sock. Mounted ONLY by the
              generator (which binds the socket) and tenant-api (which dials
              it, --generator-socket, #2903): the service's trust boundary is that mount plus
              the socket's 0600 mode plus a same-uid peer (SO_PEERCRED).
  workDir     the generator's --work-dir (disk-backed emptyDir).
  policyDir   where the deployment policy ConfigMap key is projected, at the
              SAME path in both containers (tenant-api hashes it,
              --deploy-policy, #2903).
*/}}
{{- define "tenant-api.generatorSocketDir" -}}/run/da-generator{{- end }}
{{- define "tenant-api.generatorSocketPath" -}}{{ include "tenant-api.generatorSocketDir" . }}/generator.sock{{- end }}
{{- define "tenant-api.generatorWorkDir" -}}/var/lib/da-generator{{- end }}
{{- define "tenant-api.generatorPolicyDir" -}}/etc/da-generator-policy{{- end }}
{{- define "tenant-api.generatorPolicyPath" -}}{{ include "tenant-api.generatorPolicyDir" . }}/policy.yaml{{- end }}
{{- /* The generator's /tmp emptyDir. It holds what one judgement hands
     da-read as files (the shipped platform rule files) and nothing else
     long-lived;
     the conf.d tree is in workDir. Exceeding it evicts the pod. */ -}}
{{- define "tenant-api.generatorTmpSizeLimit" -}}256Mi{{- end }}

{{/*
A byte limit as tenant-api's binary resolves it (#2902 body-limit coupling).
A number from values is that number. A string is read the way
MaxBodyBytesFromEnv / MaxBatchBodyBytesFromEnv read the env var:
strings.TrimSpace, then strconv.ParseInt(v, 10, 64) — DECIMAL, so an optional
`+` and leading zeros are accepted and `010` is ten, never octal (sprig's
int64 would read it as octal; the zeros are stripped first). Not positive,
not parseable, or past int64 → the binary's default, as the binary does.
Call with (list <value> <binary default>). Prints an integer.
*/}}
{{- define "tenant-api.effectiveBytes" -}}
{{- $v := index . 0 -}}
{{- $default := index . 1 | int64 -}}
{{- $n := 0 | int64 -}}
{{- if or (kindIs "float64" $v) (kindIs "int" $v) (kindIs "int64" $v) -}}
{{- $n = int64 $v -}}
{{- else if and (kindIs "string" $v) (regexMatch "^[+]?[0-9]+$" (trim $v)) -}}
{{- $digits := regexReplaceAll "^0+" (trimPrefix "+" (trim $v)) "" -}}
{{- /* Past int64, Go's ParseInt fails (→ default) and so does sprig's
     int64, which then yields 0 (→ default below): pinned by the
     too-long / past-int64 cases in test_generator_container.py. */ -}}
{{- if $digits -}}
{{- $n = int64 $digits -}}
{{- end -}}
{{- end -}}
{{- if gt $n 0 -}}{{ $n }}{{- else -}}{{ $default }}{{- end -}}
{{- end }}

{{/*
Generator body limits derived from tenant-api's (#2901 → #2902).

tenant-api sends the generator the files a write produces, base64-encoded in
a JSON envelope. base64 turns n bytes into 4·ceil(n/3), so a request tenant-api
accepted at its own cap can arrive at the generator about 4/3 as large; past
the generator's --max-body-bytes that write is answered 413, not judged.

  needed = 4·ceil(max(TA_MAX_BODY_BYTES, TA_MAX_BATCH_BODY_BYTES) / 3)
           + generatorJsonEnvelopeBytes

When `needed` exceeds the generator's own default, both --max-body-bytes and
--max-inflight-body-bytes are passed as `needed` (the service refuses to start
with an in-flight budget below the largest body). Otherwise neither is passed
and the generator keeps its defaults. Prints the args, one per line, or
nothing.

⛔ The two binary defaults below are copies, pinned by
tests/helm/test_generator_container.py against
components/tenant-api/internal/handler/middleware.go and
scripts/tools/ops/generator_service.py. Edit one side and the test goes red.
*/}}
{{- define "tenant-api.taDefaultMaxBodyBytes" -}}1048576{{- end }}
{{- define "tenant-api.taDefaultMaxBatchBodyBytes" -}}262144{{- end }}
{{- define "tenant-api.generatorDefaultMaxBodyBytes" -}}4194304{{- end }}
{{- define "tenant-api.generatorDefaultMaxInflightBodyBytes" -}}4194304{{- end }}
{{- /*
JSON envelope allowance, the ONE place it is written: what wraps the base64
content in a validate request — the base commit, the deadline, and per write
a path with its JSON keys and quoting (on the order of 100 bytes), so 64 KiB
is room for several hundred writes. Erring large only lets a slightly bigger
body be read; erring small turns a write tenant-api accepted into a 413.
⚠️ The derivation sizes the generator for what tenant-api RECEIVED. A write
whose resulting files are larger than its request (a small patch merged into
a large existing file) is not covered by it; the generator's own default is
the floor for that case, and a batch's resulting files can add up to more
than this derivation: tenant-api then refuses the write with 422
GENERATOR_REJECTED, reason too_large ("split the batch"; #2903).
*/ -}}
{{- define "tenant-api.generatorJsonEnvelopeBytes" -}}65536{{- end }}

{{- define "tenant-api.generatorBodyArgs" -}}
{{- $srv := .Values.tenantApi.server | default dict -}}
{{- $body := include "tenant-api.effectiveBytes" (list $srv.maxBodyBytes (include "tenant-api.taDefaultMaxBodyBytes" .)) | int64 -}}
{{- $batch := include "tenant-api.effectiveBytes" (list $srv.maxBatchBodyBytes (include "tenant-api.taDefaultMaxBatchBodyBytes" .)) | int64 -}}
{{- $largest := max $body $batch | int64 -}}
{{- $needed := add (mul 4 (div (add $largest 2) 3)) (include "tenant-api.generatorJsonEnvelopeBytes" . | int64) | int64 -}}
{{- if gt $needed (include "tenant-api.generatorDefaultMaxBodyBytes" . | int64) }}
- --max-body-bytes={{ $needed }}
{{- if gt $needed (include "tenant-api.generatorDefaultMaxInflightBodyBytes" . | int64) }}
- --max-inflight-body-bytes={{ $needed }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Generator guard (#2902). Renders nothing; fails the render on a
configuration the generator container cannot run in. The container is
required since #2903, so this always applies.
*/}}
{{- define "tenant-api.generatorGuard" -}}
{{- $g := .Values.generator -}}
{{- /* ADR-036 §3: git refuses a repository another uid owns ("dubious
     ownership"), so tenant-api and the generator must run as ONE uid. Both
     inherit it from the pod; without a pod-level runAsUser each container
     runs as its image's USER (da-tools: 10001), and the two differ. */ -}}
{{- if not (.Values.podSecurityContext | default dict).runAsUser -}}
{{- fail "the generator container (required, #2903) needs podSecurityContext.runAsUser: tenant-api and the generator container must run as the same uid (git refuses a repository another uid owns, ADR-036 §3), and without a pod-level uid each runs as its own image's user (#2902)." -}}
{{- end -}}
{{- /* ADR-036 §3: the generator reads only git objects; "git 為必要". An
     emptyDir conf.d with no gitRepoUrl never holds a repository, so the
     container could only crash-loop (and keep the pod out of Ready). */ -}}
{{- /* Before the repository check: a removed confDir.type=hostPath, or an
     existingClaim the type ignores, has to read as that, not as "no
     repository". */ -}}
{{- include "tenant-api.confDirGuard" . -}}
{{- if and (not .Values.gitRepoUrl) (ne .Values.confDir.type "pvc") -}}
{{- fail "the generator container (required, #2903) needs a git repository in conf.d: set gitRepoUrl, or confDir.type=pvc (optionally confDir.pvc.existingClaim) holding an existing repository. An emptyDir conf.d without gitRepoUrl is never a repository, and the generator reads only git objects (ADR-036 §3, #2902)." -}}
{{- end -}}
{{- /* ADR-036 §3: the container's request equals its limit. */ -}}
{{- $req := ($g.resources | default dict).requests | default dict -}}
{{- $lim := ($g.resources | default dict).limits | default dict -}}
{{- if or (not $req.cpu) (not $req.memory) (not $lim.cpu) (not $lim.memory) -}}
{{- fail "generator.resources must set requests and limits for both cpu and memory (ADR-036 §3: request = limit, 512Mi / 1 CPU by default) (#2902)." -}}
{{- end -}}
{{- if or (ne (toString $req.cpu) (toString $lim.cpu)) (ne (toString $req.memory) (toString $lim.memory)) -}}
{{- fail (printf "generator.resources.requests must equal generator.resources.limits (ADR-036 §3: request = limit), written the same way; got requests cpu=%v memory=%v, limits cpu=%v memory=%v (#2902)." $req.cpu $req.memory $lim.cpu $lim.memory) -}}
{{- end -}}
{{- if and $g.policy.existingConfigMap (not $g.policy.key) -}}
{{- fail "generator.policy.existingConfigMap is set but generator.policy.key is empty: name the ConfigMap key that holds the deployment policy file (#2902)." -}}
{{- end -}}
{{- end }}

{{/*
The generator container's exec probe (#2902): GET /v1/status over the Unix
socket with the image's own python3, passing on a 200. Shared by the startup,
readiness and liveness probes so the three cannot drift.
*/}}
{{- define "tenant-api.generatorProbe" -}}
exec:
  command:
    - python3
    - -c
    - |
      import socket, sys
      s = socket.socket(socket.AF_UNIX)
      s.settimeout(4)
      s.connect("{{ include "tenant-api.generatorSocketPath" . }}")
      s.sendall(b"GET /v1/status HTTP/1.1\r\nHost: probe\r\n\r\n")
      sys.exit(0 if s.recv(64).startswith(b"HTTP/1.1 200 ") else 1)
{{- end }}

{{/*
recipe-preview container (ADR-027 §5, #1040): fixed in-pod paths and image.

  socketDir   the emptyDir holding preview.sock. Mounted ONLY by the
              recipe-preview container (which binds the socket) and tenant-api
              (which dials it, TA_RECIPE_PREVIEW_SOCKET): the service's trust
              boundary is that mount plus the socket's 0600 mode plus a
              same-uid peer (SO_PEERCRED).
  image       the tag follows generator.image.tag unless recipePreview.image.tag
              is set: one `tools/v*` release builds both images, so the one
              pin bump_docs.py --tools rewrites moves both. A digest is never
              inherited (the images differ).
*/}}
{{- define "tenant-api.recipePreviewSocketDir" -}}/run/recipe-preview{{- end }}
{{- define "tenant-api.recipePreviewSocketPath" -}}{{ include "tenant-api.recipePreviewSocketDir" . }}/preview.sock{{- end }}
{{- /* ⛔ Only the TAG is a helper; deployment.yaml writes the repository
     and digest as .Values reads, so check_image_pin_capability.py's strip
     path can attribute the container to the recipePreview.image pin. */ -}}
{{- define "tenant-api.recipePreviewTag" -}}
{{- $tag := .Values.recipePreview.image.tag | default .Values.generator.image.tag -}}
{{- if not $tag -}}
{{- fail "recipePreview.image.tag is empty and so is generator.image.tag, which it follows: set one (#1040)." -}}
{{- end -}}
{{- $tag -}}
{{- end }}

{{/*
The recipe-preview container's exec probe: GET /v1/status over the Unix socket
with the image's python, passing on a 200. The service is Python's
http.server, which answers HTTP/1.0, so the check reads the status code, not
the version. Shared by the probes so they cannot drift.
*/}}
{{- define "tenant-api.recipePreviewProbe" -}}
exec:
  command:
    - python
    - -c
    - |
      import socket, sys
      s = socket.socket(socket.AF_UNIX)
      s.settimeout(4)
      s.connect("{{ include "tenant-api.recipePreviewSocketPath" . }}")
      s.sendall(b"GET /v1/status HTTP/1.0\r\n\r\n")
      sys.exit(0 if s.recv(64).split()[1:2] == [b"200"] else 1)
{{- end }}

{{/*
The port the tenant-api container listens on: 8080. Every place the chart
names that port reads it here — the container port, its probes, the Service's
targetPort, the NetworkPolicy, the oauth2-proxy TCP upstream and both
prometheus.io/port annotations (Service and pod). The process itself listens on
tenantApi.listenAddr (TA_ADDR); the render FAILS unless that address ends in
this port, so the two cannot disagree. Prometheus' platform-components job
keeps only the endpoint on the annotated port (#3483), so a drift would drop
every tenant-api target.
*/}}
{{- define "tenant-api.apiPort" -}}
{{- $port := "8080" -}}
{{- $listen := toString .Values.tenantApi.listenAddr -}}
{{- if ne (regexFind ":[0-9]+$" $listen) (printf ":%s" $port) -}}
{{- fail (printf "tenantApi.listenAddr=%q must end in :%s: the chart's container port, probes, Service targetPort, NetworkPolicy, oauth2-proxy upstream and prometheus.io/port all name %s (tenant-api.apiPort)" $listen $port $port) -}}
{{- end -}}
{{- $port -}}
{{- end }}

{{/*
ADR-027 §2, §10: where the oauth2-proxy structured config file lives. The
same ConfigMap is mounted at the same path in the oauth2-proxy and the
tenant-api containers, so "what the proxy injects" and "what tenant-api
trusts" are one file. tests/helm/test_oauth2_proxy_alpha_config.py pins it.
*/}}
{{/*
The ConfigMap is immutable and named after a hash of its content: both
containers read the file only at start, so an in-place edit (kubectl edit, or
kubelet refreshing a mounted ConfigMap) followed by a restart of ONE container
would leave tenant-api trusting headers the running oauth2-proxy does not
inject and strip. A content change is a new object, and the pod rolls with it.
*/}}
{{- define "tenant-api.oauth2ProxyConfigMap" -}}
{{ include "tenant-api.name" . }}-oauth2-proxy-{{ include "tenant-api.oauth2ProxyConfig" . | sha256sum | trunc 10 }}
{{- end -}}
{{- define "tenant-api.oauth2ProxyConfigKey" -}}
oauth2-proxy.yaml
{{- end -}}
{{- define "tenant-api.oauth2ProxyConfigDir" -}}
/etc/oauth2-proxy/config
{{- end -}}
{{/*
The Unix socket tenant-api serves its API on (ADR-027 §2), on the human-sock
emptyDir shared with the oauth2-proxy sidecar. A constant: ADR-027 §6 removed
the humanSocket key, and nothing outside the pod names this path.
*/}}
{{- define "tenant-api.humanSocketPath" -}}
/run/ta-human/human.sock
{{- end -}}
{{- define "tenant-api.oauth2ProxyConfigPath" -}}
{{ include "tenant-api.oauth2ProxyConfigDir" . }}/{{ include "tenant-api.oauth2ProxyConfigKey" . }}
{{- end -}}
{{/* The client secret file, mounted into the oauth2-proxy container only. */}}
{{- define "tenant-api.oauth2ProxyClientSecretDir" -}}
/etc/oauth2-proxy/client-secret
{{- end -}}

{{/*
The oauth2-proxy structured config (--alpha-config). Built as a dict and
rendered with toYaml, so every value is quoted as YAML needs.

  upstreamConfig        two upstreams (ADR-027 §10): /api/ to tenant-api's
                        Unix socket, the only place it serves its API
                        (ADR-027 §2; port 8080 has only /health, /ready and
                        /metrics), and / to the portal container
                        (tenant-api.portalAddress)
  injectRequestHeaders  X-Forwarded-Email ← email, X-Forwarded-Groups ←
                        groups, plus one header per oauth2Proxy.claimHeaders
                        entry (claim name → header name); the client's copy
                        of each is stripped (preserveRequestValue defaults to
                        false)
  providers             one provider; the client secret from a file; the
                        claimHeaders claims listed in additionalClaims
  server                the listener (was --http-address)
*/}}
{{- define "tenant-api.oauth2ProxyConfig" -}}
{{- $o := .Values.oauth2Proxy -}}
{{- $upstream := printf "unix://%s" (include "tenant-api.humanSocketPath" .) -}}
{{- $headers := list
      (dict "name" "X-Forwarded-Email" "values" (list (dict "claimSource" (dict "claim" "email"))))
      (dict "name" "X-Forwarded-Groups" "values" (list (dict "claimSource" (dict "claim" "groups")))) -}}
{{- $seen := dict "x-forwarded-email" "email" "x-forwarded-groups" "groups" -}}
{{- range $claim, $header := $o.claimHeaders -}}
{{- if has $claim (list "email" "groups") -}}
{{- fail (printf "oauth2Proxy.claimHeaders.%s: email and groups are always injected (X-Forwarded-Email / X-Forwarded-Groups); list only extra claims" $claim) -}}
{{- end -}}
{{- if has $claim (list "access_token" "id_token" "refresh_token" "created_at" "expires_on") -}}
{{- fail (printf "oauth2Proxy.claimHeaders.%s: a session token or timestamp is not an identity claim; tenant-api refuses it" $claim) -}}
{{- end -}}
{{- $key := lower (toString $header) -}}
{{- if hasKey $seen $key -}}
{{- fail (printf "oauth2Proxy.claimHeaders.%s: header %s already carries %s (header names match ignoring case)" $claim (toString $header) (index $seen $key)) -}}
{{- end -}}
{{- $_ := set $seen $key $claim -}}
{{- $headers = append $headers (dict "name" (toString $header) "values" (list (dict "claimSource" (dict "claim" $claim)))) -}}
{{- end -}}
{{- $provider := dict
      "id" $o.provider
      "provider" $o.provider
      "clientID" (required "oauth2Proxy.clientID is required: with the structured config file the OAuth client ID is a value, not the OAUTH2_PROXY_CLIENT_ID Secret key (ADR-027 §10)" $o.clientID)
      "clientSecretFile" (printf "%s/client-secret" (include "tenant-api.oauth2ProxyClientSecretDir" .)) -}}
{{- with $o.scope -}}
{{- $_ := set $provider "scope" . -}}
{{- end -}}
{{- with $o.oidcIssuerUrl -}}
{{- $_ := set $provider "oidcConfig" (dict "issuerURL" .) -}}
{{- end -}}
{{- with $o.gitlabGroups -}}
{{- $_ := set $provider "gitlabConfig" (dict "group" .) -}}
{{- end -}}
{{- /* oauth2-proxy copies a claim into the session only when the provider
     lists it in additionalClaims (providers/provider_data.go); without this
     every claim header would be injected empty. */ -}}
{{- with $o.claimHeaders -}}
{{- $_ := set $provider "additionalClaims" (keys . | sortAlpha) -}}
{{- end -}}
{{- /* ADR-027 §5, §10: /api/ → tenant-api's Unix socket, everything else →
     the portal static-file container in this pod (loopback only). */ -}}
{{- $portal := printf "http://%s" (include "tenant-api.portalAddress" .) -}}
{{- $config := dict
      "upstreamConfig" (dict "upstreams" (list
        (dict "id" "api" "path" "/api/" "uri" $upstream)
        (dict "id" "portal" "path" "/" "uri" $portal)))
      "injectRequestHeaders" $headers
      "providers" (list $provider)
      "server" (dict "bindAddress" "0.0.0.0:4180") -}}
{{- toYaml $config -}}
{{- end -}}

{{/*
The portal static-file container (ADR-027 §5): it listens on the pod's
loopback only, so nothing outside the pod reaches it but through
oauth2-proxy's "/" upstream. A constant: nothing else names it.
*/}}
{{- define "tenant-api.portalAddress" -}}
127.0.0.1:8081
{{- end -}}
{{- define "tenant-api.portalConfigDir" -}}
/etc/portal-nginx
{{- end -}}
{{- define "tenant-api.portalConfigMap" -}}
{{ include "tenant-api.name" . }}-portal-nginx-{{ include "tenant-api.portalNginxConfig" . | sha256sum | trunc 10 }}
{{- end -}}

{{/*
The security headers every portal response carries. Repeated in each
location that sets add_header: nginx drops the server-level add_header
directives in a location that has its own.
*/}}
{{- define "tenant-api.portalSecurityHeaders" -}}
add_header X-Frame-Options "DENY" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'self'" always;
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
{{- end -}}

{{/*
The whole nginx configuration the portal container loads (`nginx -c`): it
serves the image's static files and nothing else. ⛔ No proxy_pass or any
other *_pass, no njs, no include but mime.types (ADR-027 §5, §9 item 3:
tests/helm/test_tenant_api_portal_container.py). The image's own
/etc/nginx/nginx.conf and conf.d are never read. Runs as the pod's non-root
uid on a read-only root filesystem: pid and temp paths under /tmp.
*/}}
{{- define "tenant-api.portalNginxConfig" -}}
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr notice;

events {
    worker_connections 256;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout;
    server_tokens off;
    sendfile on;
    client_body_temp_path /tmp/client_temp;
    proxy_temp_path /tmp/proxy_temp;
    fastcgi_temp_path /tmp/fastcgi_temp;
    uwsgi_temp_path /tmp/uwsgi_temp;
    scgi_temp_path /tmp/scgi_temp;

    server {
        listen {{ include "tenant-api.portalAddress" . }};
        server_name _;
        root /usr/share/nginx/html;
        index interactive/index.html;
        {{- include "tenant-api.portalSecurityHeaders" . | nindent 8 }}

        location ~* \.(js|css|png|svg|ico|woff2?)$ {
            expires 7d;
            add_header Cache-Control "public, immutable";
            {{- include "tenant-api.portalSecurityHeaders" . | nindent 12 }}
        }

        location ~* \.(json|yaml)$ {
            expires -1;
            add_header Cache-Control "no-cache";
            {{- include "tenant-api.portalSecurityHeaders" . | nindent 12 }}
        }

        # /api/ is oauth2-proxy's other upstream and never reaches this
        # container; if it ever did, a 404 says so instead of the SPA page.
        location /api/ {
            return 404;
        }

        location / {
            try_files $uri $uri/ /interactive/index.html;
        }

        location = /healthz {
            access_log off;
            default_type text/plain;
            return 200 "ok\n";
        }

        location ~ /\. {
            deny all;
            access_log off;
            log_not_found off;
        }

        location ~ ~$ {
            deny all;
            access_log off;
            log_not_found off;
        }
    }
}
{{- end -}}
