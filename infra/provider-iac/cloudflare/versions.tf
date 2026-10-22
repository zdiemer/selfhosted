terraform {
  required_version = "= 1.13.2"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.28.1"
    }
  }
  backend "local" {}
  # The wrapper supplies the method and key through TF_ENCRYPTION.
  encryption {
    state { enforced = true }
    plan { enforced = true }
  }
}
provider "cloudflare" {}
