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
fails the render. A string is passed through unchanged, so a malformed string
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
{{- int64 $v | toString | quote -}}
{{- else -}}
{{- $v | quote -}}
{{- end -}}
{{- end }}
