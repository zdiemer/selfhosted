{{- define "cluster-status.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cluster-status.labels" -}}
app.kubernetes.io/name: cluster-status
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "cluster-status.selectorLabels" -}}
app.kubernetes.io/name: cluster-status
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
