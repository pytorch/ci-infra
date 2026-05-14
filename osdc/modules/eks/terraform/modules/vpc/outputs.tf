output "vpc_id" {
  description = "The ID of the VPC"
  value       = aws_vpc.this.id
}

output "vpc_cidr_block" {
  description = "The CIDR block of the VPC"
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "List of IDs of private subnets"
  value       = aws_subnet.private[*].id
}

output "private_subnets_by_az" {
  description = "Map of availability zone name to private subnet ID"
  value       = { for i, s in aws_subnet.private : var.azs[i] => s.id }
}

output "public_subnet_ids" {
  description = "List of IDs of public subnets"
  value       = aws_subnet.public[*].id
}

output "nat_gateway_ids" {
  description = "List of NAT Gateway IDs"
  value       = aws_nat_gateway.this[*].id
}

output "internet_gateway_id" {
  description = "The ID of the Internet Gateway"
  value       = aws_internet_gateway.this.id
}

output "pod_cidr_associations" {
  description = "Pod CIDR associations keyed by '$${bucket}-$${az}', each value an object with bucket name, AZ, CIDR, and association ID. Keyed shape avoids fragile string-splitting downstream."
  value = {
    for key, assoc in aws_vpc_ipv4_cidr_block_association.pod :
    key => {
      bucket         = local.pod_cidr_associations[key].bucket
      az             = local.pod_cidr_associations[key].az
      cidr_block     = assoc.cidr_block
      association_id = assoc.id
    }
  }
}
