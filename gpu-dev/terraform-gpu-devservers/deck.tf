# Static website hosting for deck.devservers.io
# Plain S3 static site — no CloudFront, HTTP only
#
# After `tf apply`, upload slides with:
#   aws s3 sync presentation/ s3://<bucket>/ --delete

resource "aws_s3_bucket" "deck" {
  count  = local.effective_domain_name != "" && !local.is_subdomain ? 1 : 0
  bucket = "deck-${local.effective_domain_name}"

  tags = {
    Name        = "deck-${local.effective_domain_name}"
    Environment = local.current_config.environment
  }
}

resource "aws_s3_bucket_website_configuration" "deck" {
  count  = local.effective_domain_name != "" && !local.is_subdomain ? 1 : 0
  bucket = aws_s3_bucket.deck[0].id

  index_document { suffix = "index.html" }
  error_document { key = "index.html" }
}

resource "aws_s3_bucket_public_access_block" "deck" {
  count  = local.effective_domain_name != "" && !local.is_subdomain ? 1 : 0
  bucket = aws_s3_bucket.deck[0].id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "deck" {
  count      = local.effective_domain_name != "" && !local.is_subdomain ? 1 : 0
  bucket     = aws_s3_bucket.deck[0].id
  depends_on = [aws_s3_bucket_public_access_block.deck[0]]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadGetObject"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.deck[0].arn}/*"
    }]
  })
}

# CNAME pointing deck.devservers.io → S3 website endpoint
resource "aws_route53_record" "deck" {
  count   = local.effective_domain_name != "" && !local.is_subdomain ? 1 : 0
  zone_id = local.hosted_zone_id
  name    = "deck.${local.effective_domain_name}"
  type    = "CNAME"
  ttl     = 300
  records = [aws_s3_bucket_website_configuration.deck[0].website_endpoint]
}

output "deck_url" {
  description = "URL for the presentation deck"
  value       = local.effective_domain_name != "" && !local.is_subdomain ? "http://deck.${local.effective_domain_name}" : null
}

output "deck_s3_bucket" {
  description = "S3 bucket name for uploading presentation files"
  value       = local.effective_domain_name != "" && !local.is_subdomain ? aws_s3_bucket.deck[0].id : null
}

output "deck_upload_command" {
  description = "Command to upload presentation files"
  value       = local.effective_domain_name != "" && !local.is_subdomain ? "aws s3 sync presentation/ s3://${aws_s3_bucket.deck[0].id}/ --delete" : null
}
