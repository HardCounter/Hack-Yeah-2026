# One small EC2 instance for the demo. Run only by the AWS account owner.
# Creates: one security group, one key pair, one instance, one Elastic IP.
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "eu-central-1"
}

variable "ssh_public_key" {
  description = "Public half of the deploy key (ssh-keygen -t ed25519 -f infra/deploy_key)"
  type        = string
}

variable "repo_url" {
  type    = string
  default = "https://github.com/HardCounter/Hack-Yeah-2026.git"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

resource "aws_security_group" "app" {
  name_prefix = "hackyeah-demo-"
  description = "Demo app: HTTP, HTTPS, key-only SSH"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "SSH (key only)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Project = "hackyeah-demo" }
}

resource "aws_key_pair" "deploy" {
  key_name_prefix = "hackyeah-demo-"
  public_key      = var.ssh_public_key
  tags            = { Project = "hackyeah-demo" }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.large"
  key_name               = aws_key_pair.deploy.key_name
  vpc_security_group_ids = [aws_security_group.app.id]

  # No IAM role / instance profile: nothing on this machine can call the AWS API.
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  # "standard" caps the cost; the T3 default ("unlimited") can add a surcharge under sustained load.
  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  user_data = <<-EOT
    #!/bin/bash
    set -euxo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y docker.io docker-compose-v2 git
    systemctl enable --now docker
    usermod -aG docker ubuntu
    git clone ${var.repo_url} /opt/app
    chown -R ubuntu:ubuntu /opt/app
  EOT

  tags = {
    Name    = "hackyeah-demo"
    Project = "hackyeah-demo"
  }
}

resource "aws_eip" "app" {
  domain   = "vpc"
  instance = aws_instance.app.id
  tags     = { Project = "hackyeah-demo" }
}

output "public_ip" {
  value = aws_eip.app.public_ip
}

output "site_host" {
  description = "Hostname for SITE_HOST and the DEPLOY_HOST secret (sslip.io resolves it to the IP)"
  value       = "${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io"
}
