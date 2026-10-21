package main

import (
 "context"
 "log"
 "github.com/containerd/nri/pkg/api"
 "github.com/containerd/nri/pkg/stub"
)

type plugin struct{}

func adjustment(pod *api.PodSandbox, ctr *api.Container) *api.ContainerAdjustment {
 if pod.Namespace != "games" || pod.Labels["app.kubernetes.io/name"] != "steam-headless" || pod.Annotations["steam.zachd/input-policy"] != "virtual-input-v1" {
  return nil
 }
 if ctr.Name != "steam-headless" && ctr.Name != "input-devices" { return nil }
 // Dynamic input minors cannot be expressed with static Kubernetes devices.
 // This grants only the input character-device class, never block devices.
 a := &api.ContainerAdjustment{Linux: &api.LinuxContainerAdjustment{Resources: &api.LinuxResources{Devices: []*api.LinuxDeviceCgroup{
  {Allow:true, Type:"c", Major:&api.OptionalInt64{Value:13}, Access:"rwm"},
 }}}}
 a.AddEnv("STEAM_INPUT_POLICY", "virtual-input-v1")
 return a
}

func (p *plugin) CreateContainer(_ context.Context, pod *api.PodSandbox, ctr *api.Container) (*api.ContainerAdjustment, []*api.ContainerUpdate, error) {
 a := adjustment(pod, ctr)
 if a != nil { log.Printf("grant input device class to %s/%s container %s", pod.Namespace,pod.Name,ctr.Name) }
 return a,nil,nil
}

func main() {
 s,err := stub.New(&plugin{}, stub.WithPluginName("steam-input"), stub.WithPluginIdx("80"))
 if err != nil { log.Fatal(err) }
 if err=s.Run(context.Background());err!=nil {log.Fatal(err)}
}
