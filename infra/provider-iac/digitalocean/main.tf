variable "droplets" {
  description = "Reviewed API inventory; never fill unknown properties with guesses."
  type = map(object({
    id            = string
    name          = string
    image         = string
    region        = string
    size          = string
    ipv4          = string
    ipv6          = bool
    vpc_uuid      = string
    tags          = set(string)
    volume_ids    = set(string)
    backup_policy = object({ plan = string, weekday = optional(string), hour = number })
  }))
  validation {
    condition     = toset(keys(var.droplets)) == toset(["whatnow", "egress"])
    error_message = "Inventory must contain exactly whatnow and egress."
  }
  validation {
    condition     = alltrue([for d in var.droplets : contains(["daily", "weekly"], d.backup_policy.plan)])
    error_message = "Explicitly preserve the existing daily/weekly backup policy."
  }
}

variable "ssh_source_cidrs" {
  type        = set(string)
  description = "Verified operator public addresses; Tailscale CGNAT addresses do not belong here."
  validation {
    condition = length(var.ssh_source_cidrs) > 0 && alltrue([
      for cidr in var.ssh_source_cidrs : can(cidrhost(cidr, 0)) && !contains(["0.0.0.0/0", "::/0"], cidr)
    ])
    error_message = "Supply nonempty reviewed SSH source CIDRs, never a global allow rule."
  }
}

variable "firewall_ids" {
  description = "Existing dedicated firewall IDs. Empty only after verifying no firewall applies by ID OR tag."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for key in keys(var.firewall_ids) : contains(["whatnow", "egress"], key)])
    error_message = "Firewall keys must refer to the two managed droplets."
  }
}

locals {
  whatnow    = yamldecode(file("${path.module}/../../whatnow-droplet/config.yml"))
  cloudflare = concat(local.whatnow.cloudflare_ipv4, local.whatnow.cloudflare_ipv6)
  internet   = ["0.0.0.0/0", "::/0"]
  ingress = {
    whatnow = [
      { protocol = "tcp", ports = "22", sources = var.ssh_source_cidrs },
      { protocol = "tcp", ports = "80", sources = local.cloudflare },
      { protocol = "tcp", ports = "443", sources = local.cloudflare },
      { protocol = "icmp", ports = null, sources = local.internet }
    ]
    egress = [
      { protocol = "tcp", ports = "22", sources = var.ssh_source_cidrs },
      { protocol = "tcp", ports = "80", sources = local.internet },
      { protocol = "tcp", ports = "443", sources = local.internet },
      { protocol = "tcp", ports = "25565", sources = local.internet },
      { protocol = "udp", ports = "3478", sources = local.internet },
      { protocol = "udp", ports = "49152-49500", sources = local.internet },
      { protocol = "icmp", ports = null, sources = local.internet }
    ]
  }
}


resource "digitalocean_droplet" "managed" {
  for_each    = var.droplets
  name        = each.value.name
  image       = each.value.image
  region      = each.value.region
  size        = each.value.size
  ipv6        = each.value.ipv6
  vpc_uuid    = each.value.vpc_uuid
  tags        = each.value.tags
  volume_ids  = each.value.volume_ids
  resize_disk = false
  backups     = true
  backup_policy {
    plan    = each.value.backup_policy.plan
    weekday = each.value.backup_policy.weekday
    hour    = each.value.backup_policy.hour
  }
  lifecycle {
    prevent_destroy = true
    # Bootstrap inputs and installed agents remain host-managed after adoption.
    ignore_changes = [image, ssh_keys, user_data, monitoring, droplet_agent]
    postcondition {
      condition     = self.ipv4_address == each.value.ipv4
      error_message = "Imported droplet address differs from the reviewed inventory."
    }
  }
}


resource "digitalocean_firewall" "managed" {
  for_each    = var.droplets
  name        = "selfhosted-${each.key}"
  droplet_ids = [digitalocean_droplet.managed[each.key].id]
  dynamic "inbound_rule" {
    for_each = local.ingress[each.key]
    content {
      protocol         = inbound_rule.value.protocol
      port_range       = inbound_rule.value.ports
      source_addresses = inbound_rule.value.sources
    }
  }
  # Preserve Internet egress for Squid, TURN, updates and Tailscale. The host
  # enforces service-specific private/metadata denial; this is ingress hardening.
  dynamic "outbound_rule" {
    for_each = { tcp = "1-65535", udp = "1-65535", icmp = null }
    content {
      protocol              = outbound_rule.key
      port_range            = outbound_rule.value
      destination_addresses = local.internet
    }
  }
  lifecycle { prevent_destroy = true }
}
