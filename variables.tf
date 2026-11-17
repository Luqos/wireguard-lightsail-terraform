variable "region" {
  description = "AWS Region for the VPN server. ap-southeast-5 = Asia Pacific (Malaysia), an opt-in Region."
  type        = string
  default     = "ap-southeast-5"
}

variable "availability_zone_suffix" {
  description = "Zone letter appended to the Region, e.g. \"a\" -> ap-southeast-5a."
  type        = string
  default     = "a"
}

variable "name" {
  description = "Instance name. Must not clash with an existing Lightsail instance in the Region."
  type        = string
  default     = "wg-vpn"
}

variable "blueprint_id" {
  description = "Lightsail OS image. Check with: aws lightsail get-blueprints --region <region>"
  type        = string
  default     = "ubuntu_24_04"
}

variable "bundle_id" {
  description = "Lightsail plan. Check the USD 5 IPv4 bundle ID with: aws lightsail get-bundles --region <region>"
  type        = string
  default     = "nano_3_1"
}

variable "alert_email" {
  description = "Email for the transfer alarm (create and verify the contact method first, see alerts.tf). Empty disables the alarm. Keep it in terraform.tfvars (git-ignored)."
  type        = string
  default     = ""
}

variable "alarm_out_bytes_per_5min" {
  description = "Outbound bytes per 5 minutes that counts as heavy. Default 30 MiB: half of 512 GB (in + out both count) spread over 30 days."
  type        = number
  default     = 31457280
}

variable "ssh_cidrs" {
  description = "Extra IPv4 CIDRs allowed to SSH (e.g. [\"203.0.113.7/32\"]). Browser SSH from the Lightsail console always works."
  type        = list(string)
  default     = []
}
