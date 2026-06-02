output "vpc_id" {
  description = "The ID of the VPC"
  value       = aws_vpc.this.id
}

output "vpc_cidr_block" {
  description = "The CIDR block of the VPC"
  value       = aws_vpc.this.cidr_block
}

output "vpc_ipv6_cidr_block" {
  description = "The IPv6 CIDR block of the VPC"
  value       = aws_vpc.this.ipv6_cidr_block
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

output "nat_gateway_public_ips" {
  description = "Map of NAT Gateway index (0-based) to list of public IPv4 addresses (primary first, then secondaries)."
  value = {
    for nat_idx in range(local.nat_gateway_count) :
    "nat-${nat_idx + 1}" => concat(
      [aws_eip.nat[nat_idx].public_ip],
      [for sec_key, sec in local.nat_secondary_eips :
        aws_eip.nat_secondary[sec_key].public_ip
      if sec.nat_idx == nat_idx]
    )
  }
}

output "internet_gateway_id" {
  description = "The ID of the Internet Gateway"
  value       = aws_internet_gateway.this.id
}

output "egress_only_internet_gateway_id" {
  description = "The ID of the Egress-Only Internet Gateway (IPv6 outbound from private subnets)"
  value       = aws_egress_only_internet_gateway.this.id
}
