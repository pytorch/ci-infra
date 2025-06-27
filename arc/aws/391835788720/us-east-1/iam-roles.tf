resource "aws_iam_role" "pytorch_ci_admins" {
  name = "pytorch-ci-admins"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::391835788720:user/tha@linuxfoundation.org"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Environment = var.arc_prod_environment
  }
}

resource "aws_iam_role_policy_attachment" "pytorch_ci_admins_eks_cluster" {
  role       = aws_iam_role.pytorch_ci_admins.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "pytorch_arc_admins_eks_service" {
  role       = aws_iam_role.pytorch_ci_admins.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSServicePolicy"
}
