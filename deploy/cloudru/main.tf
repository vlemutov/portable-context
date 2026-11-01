terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    cloudru = {
      source  = "cloudru/cloud"
      version = "2.1.6"
    }
  }
}

# Credentials come only from AUTH_KEY_ID / AUTH_SECRET in the installer's environment.
provider "cloudru" {
  project_id               = var.project_id
  object_storage_tenant_id = var.tenant_id
  region                   = "ru-central-1"
}

variable "project_id" { type = string }
variable "tenant_id" { type = string }
variable "name" { type = string }
variable "zone" { type = string }
variable "image_id" { type = string }
variable "flavor" { type = string }
variable "ssh_public_key" { type = string }
variable "ssh_cidr" { type = string }
variable "bucket" { type = string }
variable "disk_gb" {
  type    = number
  default = 40
  validation {
    condition     = var.disk_gb >= 20
    error_message = "Use at least 20 GB, and respect the selected image's minimum disk size."
  }
}

resource "cloudru_evolution_vpc_vpc" "memory" {
  project_id = var.project_id
  name       = var.name
}
resource "cloudru_evolution_compute_subnet" "memory" {
  project_id     = var.project_id
  name           = var.name
  zone           = { name = var.zone }
  vpc_id         = cloudru_evolution_vpc_vpc.memory.id
  subnet_address = "10.77.0.0/24"
  routed_network = true
  default        = false
}
resource "cloudru_evolution_compute_security_group" "memory" {
  project_id = var.project_id
  name       = var.name
  zone       = { name = var.zone }
}
resource "cloudru_evolution_compute_security_group_rule" "ssh" {
  security_group_id = cloudru_evolution_compute_security_group.memory.id
  direction         = "TRAFFIC_DIRECTION_INGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "22:22"
  remote_ip_prefix  = var.ssh_cidr
}
resource "cloudru_evolution_compute_security_group_rule" "outbound_tcp" {
  security_group_id = cloudru_evolution_compute_security_group.memory.id
  direction         = "TRAFFIC_DIRECTION_EGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_TCP"
  port_range        = "1:65535"
  remote_ip_prefix  = "0.0.0.0/0"
}
resource "cloudru_evolution_compute_security_group_rule" "outbound_udp" {
  security_group_id = cloudru_evolution_compute_security_group.memory.id
  direction         = "TRAFFIC_DIRECTION_EGRESS"
  ether_type        = "ETHER_TYPE_IPV4"
  ip_protocol       = "IP_PROTOCOL_UDP"
  port_range        = "1:65535"
  remote_ip_prefix  = "0.0.0.0/0"
}
resource "cloudru_evolution_compute_interface" "memory" {
  project_id                 = var.project_id
  name                       = var.name
  zone                       = { name = var.zone }
  subnet                     = { id = cloudru_evolution_compute_subnet.memory.id }
  security_groups            = [{ id = cloudru_evolution_compute_security_group.memory.id }]
  interface_security_enabled = true
  type                       = "INTERFACE_TYPE_REGULAR"
}
resource "cloudru_evolution_compute_external_ip" "memory" {
  project_id        = var.project_id
  name              = var.name
  zone              = { name = var.zone }
  network_interface = { id = cloudru_evolution_compute_interface.memory.id }
}
resource "cloudru_evolution_compute_disk" "memory" {
  project_id = var.project_id
  name       = var.name
  zone       = { name = var.zone }
  size       = var.disk_gb
  bootable   = true
  disk_type  = { name = "SSD" }
  image      = { id = var.image_id }
  lifecycle { prevent_destroy = true }
}
resource "cloudru_evolution_compute_vm" "memory" {
  project_id         = var.project_id
  name               = var.name
  zone               = { name = var.zone }
  flavor             = { name = var.flavor }
  network_interfaces = [{ id = cloudru_evolution_compute_interface.memory.id }]
  disks              = [{ id = cloudru_evolution_compute_disk.memory.id }]
  cloud_init_userdata = base64encode(join("\n", ["#cloud-config", yamlencode({
    users = [{
      name                = "pcadmin"
      groups              = "sudo"
      shell               = "/bin/bash"
      sudo                = ["ALL=(ALL) NOPASSWD:ALL"]
      lock_passwd         = true
      ssh_authorized_keys = [var.ssh_public_key]
    }]
    ssh_pwauth     = false
    disable_root   = true
    package_update = true
    packages       = ["python3", "python3-venv", "python3-pip", "cron"]
  })]))
  depends_on = [cloudru_evolution_compute_external_ip.memory,
    cloudru_evolution_compute_security_group_rule.outbound_tcp,
  cloudru_evolution_compute_security_group_rule.outbound_udp]
  lifecycle { prevent_destroy = true }
}
resource "cloudru_evolution_obs_bucket" "backups" {
  bucket     = var.bucket
  versioning = true
  # No public ACL or Allow policy. No automatic expiry of the user's archives.
  lifecycle { prevent_destroy = true }
}
resource "cloudru_evolution_iam_service_account" "backup" {
  name   = "${var.name}-backup"
  target = { project_id = var.project_id }
}
resource "cloudru_evolution_iam_permission" "backup" {
  for_each        = toset(["s3e.viewer", "s3e.editor"])
  subject_id      = cloudru_evolution_iam_service_account.backup.id
  resource_id     = cloudru_evolution_obs_bucket.backups.uuid
  role_identifier = { role_name = each.value }
}
resource "cloudru_evolution_iam_access_key" "backup" {
  service_account_id = cloudru_evolution_iam_service_account.backup.id
  description        = "Portable Context backups only"
  ttl                = "0"
}
output "host" { value = cloudru_evolution_compute_external_ip.memory.ip_address }
output "backup" {
  sensitive = true
  value = {
    bucket     = cloudru_evolution_obs_bucket.backups.bucket
    endpoint   = "https://s3.cloud.ru"
    region     = "ru-central-1"
    access_key = "${var.tenant_id}:${cloudru_evolution_iam_access_key.backup.key_id}"
    secret_key = cloudru_evolution_iam_access_key.backup.secret
  }
}
