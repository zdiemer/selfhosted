variable "policy_path" {
  type        = string
  description = "Absolute path to the complete exported and reviewed tailnet HuJSON policy."
  validation {
    condition     = fileexists(var.policy_path)
    error_message = "Export the full current policy before importing it; no guessed/default ACL."
  }
}

import {
  to = tailscale_acl.managed
  id = "acl"
}

resource "tailscale_acl" "managed" {
  acl                        = file(var.policy_path)
  overwrite_existing_content = false
  reset_acl_on_destroy       = false
  lifecycle { prevent_destroy = true }
}
