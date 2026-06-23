# Active sweeper: EventBridge fires the callback lambda on a fixed schedule so it
# can scan the Redis ZSET of in-progress jobs and time out any "zombie" jobs whose
# expected-timeout score has elapsed. The callback handler routes on the constant
# payload below to branch into the cleanup logic.

resource "aws_cloudwatch_event_rule" "sweeper" {
  name                = "crcr-sweeper-${var.environment}"
  description         = "Periodic trigger for the cross-repo-ci callback lambda to reap timed-out jobs"
  schedule_expression = "rate(${var.sweeper_interval_minutes} minutes)"
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "sweeper" {
  rule      = aws_cloudwatch_event_rule.sweeper.name
  target_id = "crcr-callback-sweeper"
  arn       = aws_lambda_function.callback.arn

  # Fixed payload the callback router uses to distinguish the cron signal from
  # a normal HUD callback HTTP invocation.
  input = jsonencode({
    source = "crcr.sweeper"
  })
}

resource "aws_lambda_permission" "sweeper_invoke" {
  statement_id  = "AllowEventBridgeSweeperInvoke"
  function_name = aws_lambda_function.callback.function_name
  action        = "lambda:InvokeFunction"
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweeper.arn
}
