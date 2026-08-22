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
