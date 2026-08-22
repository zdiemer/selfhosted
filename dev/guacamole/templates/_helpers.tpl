{{- define "guacamole.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "guacamole.labels" -}}
app.kubernetes.io/name: guacamole
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "guacamole.selectorLabels" -}}
app.kubernetes.io/name: guacamole
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
