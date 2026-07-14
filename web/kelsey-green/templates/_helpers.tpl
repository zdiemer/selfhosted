{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "kelsey-green.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "kelsey-green.labels" -}}
app.kubernetes.io/name: kelsey-green
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "kelsey-green.selectorLabels" -}}
app.kubernetes.io/name: kelsey-green
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Selector labels for the cloudflared connector, which scales separately from the
web pods and must not be picked up by the site Service.
*/}}
{{- define "kelsey-green.cloudflaredSelectorLabels" -}}
app.kubernetes.io/name: kelsey-green-cloudflared
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
