{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "cloud-game.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "cloud-game.labels" -}}
app.kubernetes.io/name: cloud-game
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "cloud-game.selectorLabels" -}}
app.kubernetes.io/name: cloud-game
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Install prefix inside the image (upstream Dockerfile WORKDIR).
*/}}
{{- define "cloud-game.home" -}}/usr/local/share/cloud-game{{- end -}}

{{/*
ICE server env vars, shared by the coordinator and the worker.

BOTH need them, for different reasons: the coordinator hands the list to the
browser in its INIT message (that is where the browser's TURN credentials come
from), and the worker gathers its own candidates with it.

koanf's env loader turns ICESERVERS[0]_URLS into iceservers.0.urls and its
arrayify() converts the numeric-keyed map into the []IceServer the config
struct wants — which is the only way to express a list of structs in env vars.

The credential never appears in values.yaml: entries that set
`credentialSecret: true` read it from the chart Secret instead.
*/}}
{{- define "cloud-game.iceServersEnv" -}}
{{- range $i, $srv := .Values.webrtc.iceServers }}
- name: CLOUD_GAME_WEBRTC_ICESERVERS[{{ $i }}]_URLS
  value: {{ $srv.urls | quote }}
{{- with $srv.username }}
- name: CLOUD_GAME_WEBRTC_ICESERVERS[{{ $i }}]_USERNAME
  value: {{ . | quote }}
{{- end }}
{{- if $srv.credentialSecret }}
- name: CLOUD_GAME_WEBRTC_ICESERVERS[{{ $i }}]_CREDENTIAL
  valueFrom:
    secretKeyRef:
      name: {{ include "cloud-game.fullname" $ }}-turn-creds
      key: credential
{{- end }}
{{- end }}
{{- end -}}
