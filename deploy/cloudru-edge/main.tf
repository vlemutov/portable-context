terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    cloudru = {
      source  = "cloudru/cloud"
      version = "2.1.6"
    }
  }
}
provider "cloudru" { project_id = var.project_id }
variable "project_id" { type = string }
variable "security_group_id" { type = string }
variable "origin_port" {
  type    = number
  default = 9443
  validation {
    condition     = var.origin_port >= 1024 && var.origin_port <= 65535 && floor(var.origin_port) == var.origin_port
    error_message = "Use an unprivileged TCP origin port."
  }
}
# This is a separate state: it cannot replace/delete the existing VM, disk or S3.
# The listener MUST already enforce mTLS before this ingress rule is applied.
resource "cloudru_evolution_compute_security_group_rule" "gateway_origin" {
  security_group_id = var.security_group_id
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "${var.origin_port}:${var.origin_port}"
  remote_ip_prefix  = "0.0.0.0/0"
}
