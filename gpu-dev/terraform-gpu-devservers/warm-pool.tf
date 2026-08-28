# Warm pool: scheduled tick that keeps a standby of pre-booted, unclaimed pods
# per GPU type so reservations can be claimed instantly. The reconcile logic
# lives in the reservation processor (it reuses create_pod); this just pings it.
# Tier counts default in code (WARM_POOL_TARGETS); override via the lambda env.
resource "aws_cloudwatch_event_rule" "warm_pool_reconcile" {
  name                = "${var.prefix}-warm-pool-reconcile"
  description         = "Reconcile the warm pod pool every minute"
  schedule_expression = "rate(1 minute)"

  tags = {
    Name        = "${var.prefix}-warm-pool-reconcile"
    Environment = local.current_config.environment
  }
}

resource "aws_cloudwatch_event_target" "warm_pool_reconcile_target" {
  rule      = aws_cloudwatch_event_rule.warm_pool_reconcile.name
  target_id = "WarmPoolReconcileTarget"
  arn       = aws_lambda_function.reservation_processor.arn
  input = jsonencode({
    warm_pool_reconcile = true
  })
}

resource "aws_lambda_permission" "allow_cloudwatch_warm_pool" {
  statement_id  = "AllowExecutionFromCloudWatchWarmPool"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reservation_processor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.warm_pool_reconcile.arn
}
