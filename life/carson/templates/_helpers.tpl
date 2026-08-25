{{/* Release name is already short and unique; nothing to derive. */}}
{{- define "carson.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "carson.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "carson.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels deliberately exclude Chart.Version: they end up in immutable
selectors, and a chart version bump would make the upgrade fail.
*/}}
{{- define "carson.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The env shared by the Deployment and the reminder CronJob. Defined once so the
two cannot drift — a CronJob pointed at a different CARSON_DB than the web pod
would "work" while writing reminders into a database nobody reads.
*/}}
{{- define "carson.env" -}}
- name: CARSON_DB
  value: /data/carson.db
{{- /* Where the reminder CronJob reaches the web pod. In-cluster ClusterIP,
       so it bypasses the ingress and Authelia entirely. */}}
- name: CARSON_API_URL
  value: http://{{ include "carson.fullname" . }}:{{ .Values.service.port }}
- name: CARSON_SMS_URL
  value: {{ .Values.sms.url | quote }}
- name: CARSON_CAL_NAME
  value: {{ .Values.calendar.name | quote }}
- name: CARSON_SMS_TO
  valueFrom:
    secretKeyRef:
      name: {{ include "carson.fullname" . }}
      key: CARSON_SMS_TO
- name: CARSON_SMS_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "carson.fullname" . }}
      key: CARSON_SMS_API_KEY
- name: CARSON_FEED_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "carson.fullname" . }}
      key: CARSON_FEED_TOKEN
{{- range $k, $v := .Values.extraEnv }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}
