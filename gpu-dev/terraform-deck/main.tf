# deck.devservers.io — standalone static site for presentation slides
#
# Separate from the main GPU devservers terraform.
# One command: tofu apply — creates bucket, DNS, and uploads all slides.

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

variable "presentation_dir" {
  description = "Path to the presentation directory"
  type        = string
  default     = "../presentation"
}

locals {
  fqdn        = "deck.${var.domain_name}"
  bucket_name = "deck-${var.domain_name}"

  content_types = {
    html = "text/html"
    css  = "text/css"
    js   = "application/javascript"
    json = "application/json"
    svg  = "image/svg+xml"
    png  = "image/png"
    jpg  = "image/jpeg"
    mp4  = "video/mp4"
    webm = "video/webm"
    ico  = "image/x-icon"
    txt  = "text/plain"
    toml = "text/plain"
    md   = "text/plain"
  }

  presentation_files = fileset(var.presentation_dir, "**/*")
}

# --- S3 bucket ---

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

# --- Upload all presentation files ---

resource "aws_s3_object" "slides" {
  for_each = local.presentation_files

  bucket       = aws_s3_bucket.deck.id
  key          = each.value
  source       = "${var.presentation_dir}/${each.value}"
  etag         = filemd5("${var.presentation_dir}/${each.value}")
  content_type = lookup(local.content_types, split(".", each.value)[length(split(".", each.value)) - 1], "application/octet-stream")
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

output "files_uploaded" {
  description = "Number of files uploaded"
  value       = length(local.presentation_files)
}
