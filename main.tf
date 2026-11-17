resource "aws_lightsail_instance" "wg" {
  name              = var.name
  availability_zone = "${var.region}${var.availability_zone_suffix}"
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id
  ip_address_type   = "dualstack"

  # First-boot script: installs wg-setup and runs "wg-setup install".
  # No keys pass through Terraform; they are generated on the server.
  user_data = templatefile("${path.module}/scripts/user_data.sh.tftpl", {
    wg_setup = file("${path.module}/scripts/wg-setup.sh")
  })

  lifecycle {
    # Changing user_data would replace the instance and wipe every peer key.
    # Rebuild deliberately with: terraform apply -replace=aws_lightsail_instance.wg
    ignore_changes = [user_data]
  }
}

resource "aws_lightsail_static_ip" "wg" {
  name = "${var.name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "wg" {
  static_ip_name = aws_lightsail_static_ip.wg.name
  instance_name  = aws_lightsail_instance.wg.name
}

# Replaces the instance's whole firewall: only these ports are open.
resource "aws_lightsail_instance_public_ports" "wg" {
  instance_name = aws_lightsail_instance.wg.name

  # WireGuard
  port_info {
    protocol   = "udp"
    from_port  = 443
    to_port    = 443
    cidrs      = ["0.0.0.0/0"]
    ipv6_cidrs = ["::/0"]
  }

  # SSH: Lightsail browser console, plus any admin IPs you list
  port_info {
    protocol          = "tcp"
    from_port         = 22
    to_port           = 22
    cidr_list_aliases = ["lightsail-connect"]
    cidrs             = length(var.ssh_cidrs) > 0 ? var.ssh_cidrs : null
  }
}
