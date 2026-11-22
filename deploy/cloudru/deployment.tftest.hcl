mock_provider "cloudru" {}

variables {
  project_id     = "11111111-1111-4111-8111-111111111111"
  tenant_id      = "22222222-2222-4222-8222-222222222222"
  image_id       = "33333333-3333-4333-8333-333333333333"
  name           = "pc-test"
  bucket         = "pc-test-backup"
  zone           = "ru.AZ-1"
  flavor         = "gen-2-4"
  ssh_cidr       = "192.0.2.1/32"
  ssh_public_key = "ssh-ed25519 TEST_ONLY"
}

run "cloud_init_and_access" {
  command = plan
  assert {
    condition     = yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).users[0].lock_passwd
    error_message = "Preserve original cloud-init for existing installations without a console password."
  }
  assert {
    condition = (
      cloudru_evolution_compute_subnet.memory.default &&
      cloudru_evolution_compute_subnet.memory.subnet_address == "10.77.0.0/24" &&
      cloudru_evolution_compute_subnet.memory.zone.name == var.zone &&
      cloudru_evolution_compute_disk.memory.zone.name == var.zone
    )
    error_message = "Create the primary subnet in the installation zone alongside its boot disk."
  }
  assert {
    condition     = yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).ssh_pwauth == false
    error_message = "Cloud-init must be base64-encoded and disable SSH passwords."
  }
  assert {
    condition     = yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).users[0].ssh_authorized_keys[0] == var.ssh_public_key
    error_message = "The generated owner's public key must reach cloud-init."
  }
  assert {
    condition     = cloudru_evolution_compute_security_group_rule.ssh.remote_ip_prefix == var.ssh_cidr
    error_message = "SSH must be restricted to the selected network."
  }
  assert {
    condition     = cloudru_evolution_obs_bucket.backups.versioning == true
    error_message = "Backup bucket versioning must be enabled."
  }
}

run "new_vm_console_access" {
  command = plan
  variables {
    console_password_hash = "synthetic-console-hash-for-template-test"
  }
  assert {
    condition = (
      yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).users[0].hashed_passwd == var.console_password_hash &&
      !yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).users[0].lock_passwd &&
      !yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).ssh_pwauth &&
      yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).disable_root &&
      yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).write_files[1].content == var.ssh_public_key &&
      yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).bootcmd[0][6] == yamldecode(base64decode(cloudru_evolution_compute_vm.memory.cloud_init_userdata)).write_files[0].path
    )
    error_message = "New guests require console-only password access and a boot check for their installation key."
  }
}
