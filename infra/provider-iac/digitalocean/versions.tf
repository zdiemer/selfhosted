terraform {
  required_version = "= 1.13.2"
  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "= 2.106.0"
    }
  }
  backend "local" {}
  # The wrapper supplies the method and key through TF_ENCRYPTION.
  encryption {
    state { enforced = true }
    plan { enforced = true }
  }
}
provider "digitalocean" {}
