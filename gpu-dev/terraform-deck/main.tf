# deck.devservers.io — static site for presentation slides
#
# S3 bucket + CloudFront (HTTPS) + ACM certificate + Route53 alias.
# One command: tofu apply — creates everything and syncs presentation/ to S3.

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

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
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

# --- ACM certificate (must be us-east-1 for CloudFront) ---

resource "aws_acm_certificate" "deck" {
  provider          = aws.us_east_1
  domain_name       = local.fqdn
  validation_method = "DNS"
  tags              = { Name = local.fqdn }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.deck.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  zone_id = var.route53_zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 60
  records = [each.value.record]
}

resource "aws_acm_certificate_validation" "deck" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.deck.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# --- CloudFront distribution ---

resource "aws_cloudfront_distribution" "deck" {
  enabled             = true
  default_root_object = "index.html"
  aliases             = [local.fqdn]
  price_class         = "PriceClass_100" # NA + EU (cheapest)
  http_version        = "http2and3"

  origin {
    domain_name = aws_s3_bucket_website_configuration.deck.website_endpoint
    origin_id   = "s3-website"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # S3 website endpoint is HTTP only
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-website"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400
  }

  # SPA: serve index.html for 404s from S3
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.deck.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  tags = { Name = local.fqdn }
}

# --- Upload presentation folder ---

resource "terraform_data" "sync_slides" {
  triggers_replace = [timestamp()]

  provisioner "local-exec" {
    command = "aws s3 sync ${var.presentation_dir} s3://${aws_s3_bucket.deck.id}/ --delete --exclude '*.tfvars*' --exclude '.terraform*' --exclude 'CLAUDE.md' --exclude 'pyproject.toml' --exclude 'title-vid-old.mp4'"
  }

  depends_on = [aws_s3_bucket_policy.deck]
}

# --- DNS (A alias to CloudFront) ---

resource "aws_route53_record" "deck" {
  zone_id = var.route53_zone_id
  name    = local.fqdn
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.deck.domain_name
    zone_id                = aws_cloudfront_distribution.deck.hosted_zone_id
    evaluate_target_health = false
  }
}

# --- Outputs ---

output "url" {
  description = "Slide deck URL"
  value       = "https://${local.fqdn}"
}

output "cloudfront_domain" {
  description = "CloudFront distribution domain"
  value       = aws_cloudfront_distribution.deck.domain_name
}

output "source_dir" {
  description = "Presentation directory synced to S3"
  value       = var.presentation_dir
}
