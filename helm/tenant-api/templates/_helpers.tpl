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
--federation-namespace flag all resolve the namespace THROUGH THIS TEMPLATE and
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
conf-dir volume definition — switches between emptyDir, hostPath, and PVC.
*/}}
{{- define "tenant-api.confDirVolume" -}}
{{- if eq .Values.confDir.type "hostPath" }}
hostPath:
  path: {{ .Values.confDir.hostPath | quote }}
  type: DirectoryOrCreate
{{- else if eq .Values.confDir.type "pvc" }}
persistentVolumeClaim:
  claimName: {{ include "tenant-api.name" . }}-conf
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
Generator container (ADR-036 §3, #2902): fixed in-pod paths. Defined once
here; deployment.yaml and the chart tests read them from the render, never
from a second copy.

  socketDir   the emptyDir holding generator.sock. Mounted ONLY by the
              generator (which binds the socket) and tenant-api (which will
              dial it, #2903): the service's trust boundary is that mount plus
              the socket's 0600 mode plus a same-uid peer (SO_PEERCRED).
  workDir     the generator's --work-dir (disk-backed emptyDir).
  policyDir   where the deployment policy ConfigMap key is projected, at the
              SAME path in both containers (tenant-api hashes it, #2903).
*/}}
{{- define "tenant-api.generatorSocketDir" -}}/run/da-generator{{- end }}
{{- define "tenant-api.generatorSocketPath" -}}{{ include "tenant-api.generatorSocketDir" . }}/generator.sock{{- end }}
{{- define "tenant-api.generatorWorkDir" -}}/var/lib/da-generator{{- end }}
{{- define "tenant-api.generatorPolicyDir" -}}/etc/da-generator-policy{{- end }}
{{- define "tenant-api.generatorPolicyPath" -}}{{ include "tenant-api.generatorPolicyDir" . }}/policy.yaml{{- end }}
{{- /* The generator's /tmp emptyDir. It holds what one judgement hands
     amtool (the generated Alertmanager config) and nothing else long-lived;
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
the floor for that case. The caller that builds those requests is #2903; a
batch's resulting files can add up to more than this derivation.
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
Generator guard (#2902). Renders nothing; fails the render when
generator.enabled is set on a configuration the container cannot run in.
*/}}
{{- define "tenant-api.generatorGuard" -}}
{{- if .Values.generator.enabled -}}
{{- $g := .Values.generator -}}
{{- /* ADR-036 §3: git refuses a repository another uid owns ("dubious
     ownership"), so tenant-api and the generator must run as ONE uid. Both
     inherit it from the pod; without a pod-level runAsUser each container
     runs as its image's USER (da-tools: 10001), and the two differ. */ -}}
{{- if not (.Values.podSecurityContext | default dict).runAsUser -}}
{{- fail "generator.enabled needs podSecurityContext.runAsUser: tenant-api and the generator container must run as the same uid (git refuses a repository another uid owns, ADR-036 §3), and without a pod-level uid each runs as its own image's user (#2902)." -}}
{{- end -}}
{{- /* ADR-036 §3: the generator reads only git objects; "git 為必要". An
     emptyDir conf.d with no gitRepoUrl never holds a repository, so the
     container could only crash-loop (and keep the pod out of Ready). */ -}}
{{- if and (not .Values.gitRepoUrl) (not (has .Values.confDir.type (list "pvc" "hostPath"))) -}}
{{- fail "generator.enabled needs a git repository in conf.d: set gitRepoUrl, or confDir.type=pvc / hostPath holding an existing repository. An emptyDir conf.d without gitRepoUrl is never a repository, and the generator reads only git objects (ADR-036 §3, #2902)." -}}
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
