{{- define "egress-proxy.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "egress-proxy.labels" -}}
app.kubernetes.io/name: egress-proxy
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels. Never include Chart.Version — a Deployment's selector is
immutable, so a chart version bump would make the release un-upgradeable.
*/}}
{{- define "egress-proxy.selectorLabels" -}}
app.kubernetes.io/name: egress-proxy
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The in-cluster proxy URL for one client, credentials inlined.

This is the single place the URL is assembled, so every consumer — the client
Secret, the README, upgrade.sh's smoke test — agrees on its shape. Scheme is
plain http: this address is only ever dialled from inside the cluster, and the
CONNECT payload is the client's own TLS regardless. The hop that leaves the
house is the one that gets wrapped (see lanes.vps.tls).
*/}}
{{- define "egress-proxy.clientUrl" -}}
{{- $pw := index .root.Values.clientPasswords .client.name | default "" -}}
{{- printf "http://%s:%s@%s.%s.svc.cluster.local:%v" .client.name $pw (include "egress-proxy.fullname" .root) .root.Release.Namespace (include "egress-proxy.clientPort" .) -}}
{{- end -}}

{{/*
The listener port for a client's lane.

This is what makes the exit visible in the URL, and it is the whole reason lanes
have ports at all: smitele-bot reduces its proxy URL to `scheme://host:port` to
key its Cloudflare clearance state, so two clients on different exits must not
share a port or they share a clearance bucket while sitting behind different
addresses. See the lanes section of values.yaml.
*/}}
{{- define "egress-proxy.clientPort" -}}
{{- $laneName := .client.lane | default "direct" -}}
{{- $lane := index .root.Values.lanes $laneName -}}
{{- if not $lane -}}
{{- fail (printf "client %q names lane %q, which is not defined under lanes" .client.name $laneName) -}}
{{- end -}}
{{- if not $lane.port -}}
{{- fail (printf "lane %q has no port. Every lane needs its own listener port — see the lanes section of values.yaml" $laneName) -}}
{{- end -}}
{{- $lane.port -}}
{{- end -}}

{{/*
Lanes that are actually listening: `direct` always, plus every enabled parent.
A disabled lane must not open a port, or a client could reach a listener with no
peer behind it and get an error that looks like a network fault.
*/}}
{{- define "egress-proxy.activeLanes" -}}
{{- $out := dict -}}
{{- range $name, $lane := .Values.lanes -}}
{{- if or (eq $lane.kind "direct") $lane.enabled -}}
{{- $_ := set $out $name $lane -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}

{{/*
Squid ACL name for a client. Squid ACL names may not contain every character a
Kubernetes name may, so normalise once, here, rather than at each use.
*/}}
{{- define "egress-proxy.aclName" -}}
{{- printf "svc_%s" (. | replace "-" "_" | replace "." "_") -}}
{{- end -}}
