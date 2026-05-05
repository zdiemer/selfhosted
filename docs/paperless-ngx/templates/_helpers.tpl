{{/*
Fully-qualified resource name. Per-component objects append their service name
(e.g. {{ include "paperless.fullname" . }}-postgres).
*/}}
{{- define "paperless.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "paperless.labels" -}}
app.kubernetes.io/name: paperless-ngx
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels shared across components (stable across upgrades — never
include Chart.Version). Each Deployment/Service adds its own
`app.kubernetes.io/component` inline so the selectors stay distinct.
*/}}
{{- define "paperless.selectorLabels" -}}
app.kubernetes.io/name: paperless-ngx
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
