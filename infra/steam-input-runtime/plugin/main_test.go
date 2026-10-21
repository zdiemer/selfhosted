package main
import("testing";"github.com/containerd/nri/pkg/api")
func TestScope(t *testing.T) {
 p:=&api.PodSandbox{Namespace:"games",Labels:map[string]string{"app.kubernetes.io/name":"steam-headless"},Annotations:map[string]string{"steam.zachd/input-policy":"virtual-input-v1"}}
 c:=&api.Container{Name:"steam-headless"}
 a:=adjustment(p,c); if a==nil {t.Fatal("approved container rejected")}
 rule:=a.Linux.Resources.Devices[0];if rule.Type!="c"||rule.Major.Value!=13||rule.Minor!=nil {t.Fatal("unexpected device permissions")}
 c.Name="unrelated";if adjustment(p,c)!=nil {t.Fatal("unrelated container granted access")}
 c.Name="input-devices";p.Namespace="other";if adjustment(p,c)!=nil {t.Fatal("other namespace granted access")}
 p.Namespace="games";delete(p.Annotations,"steam.zachd/input-policy");if adjustment(p,c)!=nil {t.Fatal("missing opt-in granted access")}
}
