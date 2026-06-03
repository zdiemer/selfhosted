{{/*
Fully-qualified resource name.
*/}}
{{- define "stirling.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "stirling.labels" -}}
app.kubernetes.io/name: stirling-pdf
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "stirling.selectorLabels" -}}
app.kubernetes.io/name: stirling-pdf
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
True when Stirling needs its login machinery turned on — either native login
or native OIDC (OIDC implies a logged-in user store).
*/}}
{{- define "stirling.loginEnabled" -}}
{{- if or .Values.stirling.login.enabled .Values.stirling.oauth2.enabled -}}true{{- else -}}false{{- end -}}
{{- end -}}
