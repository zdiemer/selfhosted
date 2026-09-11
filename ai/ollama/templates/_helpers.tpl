{{/*
Fully-qualified resource name.
*/}}
{{- define "ollama.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "ollama.labels" -}}
app.kubernetes.io/name: ollama
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "ollama.selectorLabels" -}}
app.kubernetes.io/name: ollama
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
