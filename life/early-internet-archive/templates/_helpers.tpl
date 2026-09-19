{{- define "early-internet-archive.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "early-internet-archive.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "early-internet-archive.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "early-internet-archive.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "early-internet-archive.claimName" -}}
{{- if .Values.persistence.archive.existingClaim -}}
{{- .Values.persistence.archive.existingClaim -}}
{{- else if .Values.persistence.archive.create -}}
{{- printf "%s-archive" (include "early-internet-archive.fullname" .) -}}
{{- else -}}
{{- fail "persistence.archive.existingClaim must be set when persistence.archive.create is false" -}}
{{- end -}}
{{- end -}}
