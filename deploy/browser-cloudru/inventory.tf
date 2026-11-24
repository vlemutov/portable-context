terraform {
  required_version = ">= 1.9, < 2"
  required_providers {
    cloudru = {
      source  = "cloudru/cloud"
      version = "2.1.6"
    }
  }
}

variable "project_id" { type = string }
provider "cloudru" { project_id = var.project_id }

# This prototype accepts only an isolated project with one network interface.
# Two results are sufficient to detect an ambiguous/shared target.
data "cloudru_evolution_compute_interface_collection" "project" {
  project_id = var.project_id
  page_size  = 2
}

output "interfaces" {
  value = data.cloudru_evolution_compute_interface_collection.project.interfaces
}
