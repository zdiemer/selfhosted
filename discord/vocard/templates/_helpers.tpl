{{/*
Fully-qualified name for a component. Prefixes everything with the release
name so two releases can coexist without colliding.
*/}}
{{- define "vocard.fullname" -}}
{{- printf "%s-%s" .Release.Name .componentName | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "vocard.labels" -}}
app.kubernetes.io/name: vocard
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .componentName }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (must be stable across upgrades — don't add Chart.Version).
*/}}
{{- define "vocard.selectorLabels" -}}
app.kubernetes.io/name: vocard
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .componentName }}
{{- end -}}
