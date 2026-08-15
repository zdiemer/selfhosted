{{/*
Fully-qualified resource name. Multi-component chart, but the app is the
unsuffixed one; Postgres appends -postgres.
*/}}
{{- define "rachel-freeman.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "rachel-freeman.labels" -}}
app.kubernetes.io/name: rachel-freeman
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "rachel-freeman.selectorLabels" -}}
app.kubernetes.io/name: rachel-freeman
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The Postgres DSN handed to the app. Assembled here rather than stored, so the
password lives in exactly one place (the postgres Secret) and rotating it does
not mean editing a second copy that silently drifts.
*/}}
{{- define "rachel-freeman.databaseUrl" -}}
{{- printf "postgres://%s:%s@%s-postgres:5432/%s" .Values.postgres.user .Values.postgres.password (include "rachel-freeman.fullname" .) .Values.postgres.database -}}
{{- end -}}
