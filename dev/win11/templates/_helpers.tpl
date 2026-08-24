{{/*
The VM's name. Everything else keys off it: KubeVirt derives the
VirtualMachineInstance, the virt-launcher pod and the vm.kubevirt.io/name label
from this, and the RDP Service selects on that label.
*/}}
{{- define "win11.name" -}}
{{- default .Release.Name .Values.vm.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard labels.
*/}}
{{- define "win11.labels" -}}
app.kubernetes.io/name: win11
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Annotations for every PVC this chart causes to exist.

k8up in this cluster is OPT-OUT (infra/k8up: skipWithoutAnnotation: false), so
without this annotation it would try to back up VM disks. It cannot: a
volumeMode: Block PVC has no filesystem for restic to walk, and a live VM's
disk is a torn crash-consistent image anyway. VM backups are
VirtualMachineSnapshot objects instead — see README §Backups.
*/}}
{{- define "win11.pvcAnnotations" -}}
k8up.io/backup: "false"
{{- end -}}

{{/*
XML-escape a value before it is interpolated into autounattend.xml.

Not cosmetic. Windows passwords routinely contain & and <, and an unescaped one
produces a malformed answer file — which Setup does not report as a bad
password. It ignores the file and drops to the interactive installer, so an
unattended build silently becomes a machine waiting for someone to click
"Next". Order matters: & must be replaced first or it double-escapes the
entities the later rules introduce.
*/}}
{{- define "win11.xml" -}}
{{- . | toString | replace "&" "&amp;" | replace "<" "&lt;" | replace ">" "&gt;" | replace "\"" "&quot;" | replace "'" "&apos;" -}}
{{- end -}}
