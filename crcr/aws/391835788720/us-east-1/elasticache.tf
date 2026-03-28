resource "aws_security_group" "redis" {
  name        = "${var.environment}-allow-redis"
  description = "Allow connection on port 6379 (redis)"
  vpc_id      = module.crcr_vpc.vpc_id

  ingress {
    description = "Allow connection on port 6379 (redis)"
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = [module.crcr_vpc.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

resource "random_password" "redis_password" {
  length  = 21
  special = false
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.environment}-cache-subnet"
  subnet_ids = module.crcr_vpc.private_subnets
  tags       = local.tags
}

resource "aws_elasticache_replication_group" "redis" {
  automatic_failover_enabled = false
  description                = "cross-repo-ci-relay Redis cache"
  engine                     = "redis"
  node_type                  = "cache.t3.small"
  num_node_groups            = 1
  port                       = 6379
  replicas_per_node_group    = 1
  replication_group_id       = "${var.environment}-crcr-rep-group"
  security_group_ids         = [aws_security_group.redis.id]
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  tags                       = local.tags
}

resource "aws_elasticache_cluster" "redis" {
  apply_immediately    = true
  cluster_id           = "${var.environment}-crcr-redis"
  replication_group_id = aws_elasticache_replication_group.redis.id
  tags                 = local.tags
}
