{{- define "buildkit.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "buildkit.labels" -}}
app.kubernetes.io/name: buildkit
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "buildkit.selectorLabels" -}}
app.kubernetes.io/name: buildkit
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
