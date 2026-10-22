import {
  for_each = var.droplets
  to       = digitalocean_droplet.managed[each.key]
  id       = each.value.id
}

import {
  for_each = var.firewall_ids
  to       = digitalocean_firewall.managed[each.key]
  id       = each.value
}
