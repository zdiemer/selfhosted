{{/*
Fully-qualified resource name.
*/}}
{{- define "steam-headless.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "steam-headless.labels" -}}
app.kubernetes.io/name: steam-headless
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "steam-headless.selectorLabels" -}}
app.kubernetes.io/name: steam-headless
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
