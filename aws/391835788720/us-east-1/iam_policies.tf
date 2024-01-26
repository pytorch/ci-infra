resource "aws_iam_user" "ossci" {
  name = "ossci"

  tags = {
    Project = "runners-eks"
  }
}
