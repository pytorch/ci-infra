# Deploy Lambda Functions

**Use this skill when deploying Lambda function changes to production or test environments.**

## IMPORTANT: Mac Build Issues

⚠️ **DO NOT use `aws lambda update-function-code` directly!**

Lambda functions built on Mac can have issues:
- Python `__pycache__` files with wrong architecture
- Platform-specific dependencies
- Binary incompatibilities with AWS Linux runtime

## Proper Deployment Process

### 1. Commit your changes
```bash
cd "$(git rev-parse --show-toplevel)/gpu-dev/terraform-gpu-devservers"
git add lambda/
git commit -m "fix: your change description"
git push origin HEAD  # open a PR; direct pushes to main are rejected
# NOTE: land the PR before deploying. Step 3 applies your LOCAL tree, so
# applying with the PR still open ships unreviewed code.
```

### 2. Switch to the correct workspace
```bash
# For production (us-east-2)
./switch-to.sh prod

# For test (us-west-1)
./switch-to.sh default
```

### 3. Deploy via OpenTofu
```bash
# OpenTofu will rebuild Lambda packages cleanly
tofu apply
```

**Why this works:**
- OpenTofu rebuilds Lambda ZIP files from scratch
- Avoids Mac-specific build artifacts
- Ensures consistent deployment across environments
- Properly updates Lambda function code AND configuration

## Quick Hotfix (Emergency Only)

If you absolutely must hotfix without full `tofu apply`:

```bash
# Clean Python cache first
cd lambda/reservation_processor
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Zip and deploy
zip -r /tmp/lambda.zip .
aws lambda update-function-code \
  --region us-east-2 \
  --function-name pytorch-gpu-dev-reservation-processor \
  --zip-file fileb:///tmp/lambda.zip

# Then ALWAYS follow up with proper tofu apply
```

## Lambda Functions in this Repo

- `reservation_processor` - Main reservation handler
- `availability_updater` - GPU availability tracking
- `reservation_expiry` - Pod cleanup on expiry

## Common Issues

**"Code not updating"**: Run `tofu taint` first:
```bash
tofu taint aws_lambda_function.reservation_processor
tofu apply
```

**"Import errors in Lambda"**: Mac `__pycache__` issue - use `tofu apply`

**"Function exists but code is old"**: Lambda version mismatch - check `tofu refresh`

## Never Do This

❌ Manually zip and upload Lambda code from Mac
❌ Use `aws lambda update-function-code` as primary deployment method
❌ Skip `git push` before deploying
❌ Deploy to prod without testing in default workspace first
