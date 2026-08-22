{{/*
Fully-qualified resource name. Single-component chart, so no suffix.
*/}}
{{- define "smt-imagine.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels applied to every resource.
*/}}
{{- define "smt-imagine.labels" -}}
app.kubernetes.io/name: smt-imagine
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable across upgrades — never include Chart.Version).
*/}}
{{- define "smt-imagine.selectorLabels" -}}
app.kubernetes.io/name: smt-imagine
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The <member> block every server config shares (ServerConfig in
libcomp/schema/serverconfig.xml). Takes a dict: root (.), log (file stem).
*/}}
{{- define "smt-imagine.serverConfig" -}}
{{- $v := .root.Values -}}
        <member name="DatabaseType">SQLITE3</member>
        <member name="DiffieHellmanKeyPair">{{ $v.server.diffieHellmanKeyPair }}</member>
        <member name="MultithreadMode">true</member>
        <member name="DataStore">
            <element>/var/lib/comp_hack/datastore</element>
            <element>/var/lib/comp_hack/data</element>
        </member>
        <member name="DataStoreSync">false</member>
        <member name="LogFile">/var/log/comp_hack/{{ .log }}.log</member>
        <member name="LogFileAppend">true</member>
        {{- /* LogRotation is left off: 4.12.2's rotation formats its filenames
             with a broken String::Arg ("Argument not found in string:
             %1.%2.gz") and the server exits before it logs a line. The PVC
             log grows instead; it is small and on a 2Gi claim. */}}
{{- end -}}
