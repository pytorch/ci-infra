resource "aws_iam_user" "ossci" {
  name = "ossci"

  tags = {
    Project = "runners-eks"
  }
}

data "aws_iam_user" "jschmidt" {
  user_name = "jschmidt@meta.com"
}

data "aws_iam_user" "lhyde" {
  user_name = "lhyde@linuxfoundation.org"
}
