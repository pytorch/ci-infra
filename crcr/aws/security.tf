resource "aws_security_group" "lambda" {
  name        = "crcr-lambda-sg-${var.environment}"
  description = "Security group for Lambda function"
  vpc_id      = module.crcr_vpc.vpc_id
  tags        = local.tags
}

resource "aws_security_group_rule" "lambda_to_redis" {
  type                     = "egress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.lambda.id
  source_security_group_id = aws_security_group.redis.id
  description              = "Allow Redis access"
}

resource "aws_security_group_rule" "lambda_to_https" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  security_group_id = aws_security_group.lambda.id
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "Allow HTTPS for Secrets Manager and GitHub API"
}
