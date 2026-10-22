variable "records" {
  description = "Selected existing records, keyed by stable descriptive names; preserve all other zone records."
  type = map(object({
    id      = string
    zone_id = string
    name    = string
    type    = string
    content = string
    ttl     = number
    proxied = bool
    comment = optional(string)
    tags    = optional(set(string), [])
  }))
  validation {
    condition     = length(var.records) > 0 && alltrue([for r in var.records : contains(["A", "AAAA", "CNAME"], r.type)])
    error_message = "Import explicit A/AAAA/CNAME records; do not adopt mail/verification records inadvertently."
  }
}

import {
  for_each = var.records
  to       = cloudflare_dns_record.managed[each.key]
  id       = "${each.value.zone_id}/${each.value.id}"
}

resource "cloudflare_dns_record" "managed" {
  for_each = var.records
  zone_id  = each.value.zone_id
  name     = each.value.name
  type     = each.value.type
  content  = each.value.content
  ttl      = each.value.ttl
  proxied  = each.value.proxied
  comment  = each.value.comment
  tags     = each.value.tags
  # Record settings are adopted unchanged until explicitly brought into scope.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [settings]
  }
}
