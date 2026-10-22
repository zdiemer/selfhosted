mock_provider "digitalocean" {}

variables {
  ssh_source_cidrs = ["8.8.8.8/32"]
  droplets = {
    whatnow = {
      id            = "587384488", name = "whatnow", image = "1", region = "sfo3", size = "s-1vcpu-1gb",
      ipv4          = "64.227.0.147", ipv6 = false, vpc_uuid = "test-vpc", tags = [], volume_ids = [],
      backup_policy = { plan = "weekly", weekday = "TUE", hour = 0 }
    }
    egress = {
      id            = "1234", name = "egress", image = "1", region = "sfo3", size = "s-1vcpu-1gb",
      ipv4          = "147.182.206.23", ipv6 = false, vpc_uuid = "test-vpc", tags = [], volume_ids = [],
      backup_policy = { plan = "weekly", weekday = "TUE", hour = 0 }
    }
  }
}

override_resource {
  target = digitalocean_droplet.managed["whatnow"]
  values = { id = "587384488", ipv4_address = "64.227.0.147" }
}
override_resource {
  target = digitalocean_droplet.managed["egress"]
  values = { id = "1234", ipv4_address = "147.182.206.23" }
}

run "firewall_boundaries" {
  command = plan
  assert {
    condition = alltrue([for r in digitalocean_firewall.managed["whatnow"].inbound_rule :
      r.protocol != "tcp" || !contains(["80", "443"], r.port_range) ||
    (!contains(r.source_addresses, "0.0.0.0/0") && !contains(r.source_addresses, "::/0"))])
    error_message = "Whatnow HTTP/S must not allow direct global ingress."
  }
  assert {
    condition     = alltrue([for r in digitalocean_firewall.managed["egress"].inbound_rule : r.port_range != "3129"])
    error_message = "Squid must remain absent from public ingress."
  }
  assert {
    condition = alltrue(flatten([for firewall in digitalocean_firewall.managed : [
      for r in firewall.inbound_rule : r.port_range != "22" || r.source_addresses == toset(["8.8.8.8/32"])
    ]]))
    error_message = "SSH must use only the explicit operator CIDR."
  }
  assert {
    condition     = alltrue([for d in digitalocean_droplet.managed : d.backups && !d.resize_disk])
    error_message = "Backups must stay enabled and disk growth disabled."
  }
}
