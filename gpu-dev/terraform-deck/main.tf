# deck.devservers.io — standalone static site for presentation slides
#
# This is intentionally separate from the main GPU devservers terraform.
# Anyone rolling out OSDC can skip this entirely — it's just a slide deck host.
#
# Usage:
#   1. Set your variables (domain, Route53 zone ID)
#   2. tofu init && tofu apply
#   3. aws s3 sync ../presentation/ s3://<bucket>/ --delete
#
# No dependency on the main infra state, EKS cluster, or Lambda functions.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  description = "AWS region for the S3 bucket"
  type        = string
  default     = "us-east-2"
}

variable "domain_name" {
  description = "Base domain (e.g. devservers.io). The site will be at deck.<domain>."
  type        = string
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID for the domain. Find with: aws route53 list-hosted-zones"
  type        = string
}

locals {
  fqdn        = "deck.${var.domain_name}"
  bucket_name = "deck-${var.domain_name}"
}

# --- S3 static website ---

resource "aws_s3_bucket" "deck" {
  bucket = local.bucket_name
  tags   = { Name = local.fqdn }
}

resource "aws_s3_bucket_website_configuration" "deck" {
  bucket = aws_s3_bucket.deck.id
  index_document { suffix = "index.html" }
  error_document { key = "index.html" }
}

resource "aws_s3_bucket_public_access_block" "deck" {
  bucket                  = aws_s3_bucket.deck.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "deck" {
  bucket     = aws_s3_bucket.deck.id
  depends_on = [aws_s3_bucket_public_access_block.deck]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicRead"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.deck.arn}/*"
    }]
  })
}

# --- DNS ---

resource "aws_route53_record" "deck" {
  zone_id = var.route53_zone_id
  name    = local.fqdn
  type    = "CNAME"
  ttl     = 300
  records = [aws_s3_bucket_website_configuration.deck.website_endpoint]
}

# --- Outputs ---

output "url" {
  description = "Slide deck URL"
  value       = "http://${local.fqdn}"
}

output "upload_command" {
  description = "Run this to deploy slides"
  value       = "aws s3 sync ../presentation/ s3://${local.bucket_name}/ --delete"
}

output "bucket" {
  value = local.bucket_name
}
