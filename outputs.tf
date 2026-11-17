output "static_ip" {
  description = "WireGuard endpoint IP baked into every client config."
  value       = aws_lightsail_static_ip.wg.ip_address
}

output "instance_name" {
  value = aws_lightsail_instance.wg.name
}

output "next_steps" {
  value = <<-EOT
    1. Lightsail console > ${aws_lightsail_instance.wg.name} > Connect using SSH
    2. sudo cloud-init status --wait      (wait for first-boot install to finish)
    3. sudo wg-setup add <name>           (one peer per person)
  EOT
}
