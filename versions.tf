terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.70"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.100"
    }
  }
}

provider "awscc" {
  region = var.region
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = "wireguard-vpn"
      managed-by = "terraform"
    }
  }
}
