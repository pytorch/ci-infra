resource "aws_sqs_queue" "terraform_queue" {
  name                      = local.cluster_name
  message_retention_seconds = 399
  sqs_managed_sse_enabled   = true

  tags = {
    Project     = "runners-eks"
    Environment = var.environment
    Context     = local.cluster_name
  }
}

resource "aws_sqs_queue_policy" "terraform_queue" {
  queue_url = aws_sqs_queue.terraform_queue.id
  policy    = jsonencode({
    Version   = "2008-10-17"
    Statement = {
        Effect    = "Allow"
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.terraform_queue.arn
        Principal = {
            Service = [
                "events.amazonaws.com",
                "sqs.amazonaws.com",
            ]
        }
    }
  })
}

resource "aws_cloudwatch_event_rule" "k_scheduled_change_rule" {
  name        = "KarpenterScheduledChangeRule-${local.cluster_name}"

  event_pattern = jsonencode({
    source = [
      "aws.health"
    ]
    detail-type = [
      "AWS Health Event"
    ]
  })
}

resource "aws_cloudwatch_event_target" "k_scheduled_change_rule_target" {
  rule      = aws_cloudwatch_event_rule.k_scheduled_change_rule.name
  arn       = aws_sqs_queue.terraform_queue.arn
}

resource "aws_cloudwatch_event_rule" "k_spot_interruption_r" {
  name        = "SpotInterruptionRule-${local.cluster_name}"

  event_pattern = jsonencode({
    source = [
      "aws.health"
    ]
    detail-type = [
      "EC2 Spot Instance Interruption Warning"
    ]
  })
}

resource "aws_cloudwatch_event_target" "k_spot_interruption_r_target" {
  rule      = aws_cloudwatch_event_rule.k_spot_interruption_r.name
  arn       = aws_sqs_queue.terraform_queue.arn
}

resource "aws_cloudwatch_event_rule" "k_rebalance_r" {
  name        = "RebalanceRule-${local.cluster_name}"

  event_pattern = jsonencode({
    source = [
      "aws.health"
    ]
    detail-type = [
      "EC2 Instance Rebalance Recommendation"
    ]
  })
}

resource "aws_cloudwatch_event_target" "k_rebalance_r_target" {
  rule      = aws_cloudwatch_event_rule.k_rebalance_r.name
  arn       = aws_sqs_queue.terraform_queue.arn
}

resource "aws_cloudwatch_event_rule" "k_inst_state_change_r" {
  name        = "InstanceStateChangeRule-${local.cluster_name}"

  event_pattern = jsonencode({
    source = [
      "aws.health"
    ]
    detail-type = [
      "EC2 Instance State-change Notification"
    ]
  })
}

resource "aws_cloudwatch_event_target" "k_inst_state_change_r_target" {
  rule      = aws_cloudwatch_event_rule.k_inst_state_change_r.name
  arn       = aws_sqs_queue.terraform_queue.arn
}
