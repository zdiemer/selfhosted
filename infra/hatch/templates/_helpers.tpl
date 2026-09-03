{{- define "hatch.fullname" -}}
{{- .Release.Name -}}
{{- end -}}

{{- define "hatch.labels" -}}
app.kubernetes.io/name: {{ include "hatch.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/*
No chart version here — selectors are immutable, and a version bump would make
the Deployment unpatchable.
*/}}
{{- define "hatch.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hatch.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The effective action deny-list: what the operator wrote, plus this release
itself. Injected rather than documented, because "don't restart hatch with
hatch" is exactly the rule an agent would reason its way around — restarting
itself kills the request mid-flight, so no audit line is ever written and the
caller sees a connection reset it cannot tell apart from a crash.
*/}}
{{- define "hatch.denyWorkloads" -}}
{{- $deny := concat (.Values.actions.denyWorkloads | default list) (list (include "hatch.fullname" .) (printf "%s/%s" .Release.Namespace (include "hatch.fullname" .))) -}}
{{- $deny | uniq | toJson -}}
{{- end -}}

{{/*
Shared env, so anything that runs this image sees identical configuration.
*/}}
{{- define "hatch.env" -}}
- name: HATCH_PORT_NUMBER
  value: {{ .Values.service.port | quote }}
- name: HATCH_RELEASE_NAMESPACE
  value: {{ .Release.Namespace | quote }}
- name: HATCH_RELEASE_NAME
  value: {{ include "hatch.fullname" . | quote }}
- name: HATCH_POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: HATCH_ACCEPT_X_API_KEY
  value: {{ .Values.auth.acceptXApiKey | quote }}
- name: HATCH_LOGS_ENABLED
  value: {{ .Values.logs.enabled | quote }}
- name: HATCH_LOGS_DENY_NAMESPACES
  value: {{ .Values.logs.denyNamespaces | toJson | quote }}
- name: HATCH_LOGS_MAX_TAIL_LINES
  value: {{ .Values.logs.maxTailLines | quote }}
- name: HATCH_LOGS_DEFAULT_TAIL_LINES
  value: {{ .Values.logs.defaultTailLines | quote }}
- name: HATCH_LOGS_MAX_BYTES
  value: {{ .Values.logs.maxBytes | quote }}
- name: HATCH_LOGS_REDACT_ENABLED
  value: {{ .Values.logs.redact.enabled | quote }}
- name: HATCH_LOGS_REDACT_PATTERNS
  value: {{ .Values.logs.redact.patterns | toJson | quote }}
- name: HATCH_CLUSTER_STATUS_ENABLED
  value: {{ .Values.clusterStatus.enabled | quote }}
- name: HATCH_CLUSTER_STATUS_URL
  value: {{ .Values.clusterStatus.url | quote }}
- name: HATCH_CLUSTER_STATUS_CACHE_SECONDS
  value: {{ .Values.clusterStatus.cacheSeconds | quote }}
- name: HATCH_CLUSTER_STATUS_TIMEOUT_SECONDS
  value: {{ .Values.clusterStatus.timeoutSeconds | quote }}
- name: HATCH_CLUSTER_STATUS_DEFAULT_SECTIONS
  value: {{ .Values.clusterStatus.defaultSections | toJson | quote }}
- name: HATCH_GRAFANA_ENABLED
  value: {{ .Values.grafana.enabled | quote }}
- name: HATCH_GRAFANA_URL
  value: {{ .Values.grafana.url | quote }}
- name: HATCH_GRAFANA_PROM_UID
  value: {{ .Values.grafana.datasourceUids.prometheus | quote }}
- name: HATCH_GRAFANA_LOKI_UID
  value: {{ .Values.grafana.datasourceUids.loki | quote }}
- name: HATCH_GRAFANA_VERIFY_DATASOURCES
  value: {{ .Values.grafana.verifyDatasources | quote }}
- name: HATCH_GRAFANA_QUERY_MODE
  value: {{ .Values.grafana.queryMode | quote }}
- name: HATCH_GRAFANA_TIMEOUT_SECONDS
  value: {{ .Values.grafana.timeoutSeconds | quote }}
- name: HATCH_GRAFANA_MAX_RANGE_POINTS
  value: {{ .Values.grafana.maxRangePoints | quote }}
- name: HATCH_GRAFANA_MAX_LOOKBACK_HOURS
  value: {{ .Values.grafana.maxLookbackHours | quote }}
- name: HATCH_GRAFANA_LOGS_DEFAULT_LIMIT
  value: {{ .Values.grafana.logs.defaultLimit | quote }}
- name: HATCH_GRAFANA_LOGS_MAX_LIMIT
  value: {{ .Values.grafana.logs.maxLimit | quote }}
- name: HATCH_ACTIONS_ENABLED
  value: {{ .Values.actions.enabled | quote }}
- name: HATCH_ACTIONS_RESTART_ENABLED
  value: {{ .Values.actions.restart.enabled | quote }}
- name: HATCH_ACTIONS_DELETE_POD_ENABLED
  value: {{ .Values.actions.deletePod.enabled | quote }}
- name: HATCH_ACTIONS_SCALE_ENABLED
  value: {{ .Values.actions.scale.enabled | quote }}
- name: HATCH_ACTIONS_SCALE_MIN
  value: {{ .Values.actions.scale.min | quote }}
- name: HATCH_ACTIONS_SCALE_MAX
  value: {{ .Values.actions.scale.max | quote }}
- name: HATCH_ACTIONS_DENY_WORKLOADS
  value: {{ include "hatch.denyWorkloads" . | quote }}
- name: HATCH_ACTIONS_DENY_NAMESPACES
  value: {{ .Values.actions.denyNamespaces | toJson | quote }}
- name: HATCH_ACTIONS_COOLDOWN_ENABLED
  value: {{ .Values.actions.cooldown.enabled | quote }}
- name: HATCH_ACTIONS_COOLDOWN_SECONDS
  value: {{ .Values.actions.cooldown.seconds | quote }}
- name: HATCH_ACTIONS_NOTIFY_ENABLED
  value: {{ .Values.actions.notify.enabled | quote }}
- name: HATCH_ACTIONS_NOTIFY_URL
  value: {{ .Values.actions.notify.url | quote }}
- name: HATCH_ACTIONS_NOTIFY_EVENTS
  value: {{ .Values.actions.notify.events | toJson | quote }}
- name: HATCH_RBAC_ACT_MODE
  value: {{ .Values.rbac.act.mode | quote }}
- name: HATCH_RBAC_ACT_ALLOW_NAMESPACES
  value: {{ .Values.rbac.act.allowNamespaces | toJson | quote }}
- name: HATCH_AUDIT_RING_SIZE
  value: {{ .Values.audit.ringSize | quote }}
- name: HATCH_AUDIT_SOURCE
  value: {{ .Values.audit.source | quote }}
- name: HATCH_API_KEYS
  valueFrom:
    secretKeyRef:
      name: {{ include "hatch.fullname" . }}
      key: HATCH_API_KEYS
- name: HATCH_GRAFANA_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "hatch.fullname" . }}
      key: HATCH_GRAFANA_TOKEN
{{- range $k, $v := .Values.extraEnv }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end -}}
