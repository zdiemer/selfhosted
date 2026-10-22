terraform {
  required_version = "= 1.13.2"
  required_providers {
    tailscale = {
      source  = "tailscale/tailscale"
      version = "= 0.29.2"
    }
  }
  backend "local" {}
  # The wrapper supplies the method and key through TF_ENCRYPTION.
  encryption {
    state { enforced = true }
    plan { enforced = true }
  }
}
provider "tailscale" {}
