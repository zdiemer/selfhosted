{{/*
Fully-qualified resource name.
*/}}
{{- define "claude-workspace.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "claude-workspace.labels" -}}
app.kubernetes.io/name: claude-workspace
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "claude-workspace.selectorLabels" -}}
app.kubernetes.io/name: claude-workspace
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Secret holding the three op-session.sh credential files.

Prefers one created out-of-band, so the account password never passes through
helm values and therefore never lands in sh.helm.release.v1.* — see the note in
values.yaml. Falls back to the chart-rendered name for a first bootstrap.
*/}}
{{- define "claude-workspace.opSecretName" -}}
{{- if .Values.secrets.onePassword.existingSecret -}}
{{ .Values.secrets.onePassword.existingSecret }}
{{- else -}}
{{ include "claude-workspace.fullname" . }}-1password
{{- end -}}
{{- end -}}
