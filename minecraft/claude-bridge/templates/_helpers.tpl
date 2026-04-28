{{- define "claude-bridge.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "claude-bridge.labels" -}}
app.kubernetes.io/name: claude-bridge
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "claude-bridge.selectorLabels" -}}
app.kubernetes.io/name: claude-bridge
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
