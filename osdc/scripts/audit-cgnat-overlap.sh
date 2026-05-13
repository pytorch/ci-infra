#!/usr/bin/env bash
# CGNAT overlap audit for OSDC IPv4 capacity expansion (INCREASE_IPV4.md Phase 0).
#
# Audits AWS for any existing TGW/peering/route-table CIDRs that overlap the
# 100.64.0.0/10 CGNAT range OSDC will use for pod IPs. Any overlap would
# silently break pod-to-peer connectivity once VPC CNI Custom Networking
# activates (PR 7).
#
# Run per region. Re-run before PR 7 cutover and whenever a new
# region/account is added.
#
# Usage:
#   AWS_PROFILE=<profile> ./audit-cgnat-overlap.sh <region>
#
# Pass criteria: zero "HIT:" lines in the output.
set -euo pipefail

REGION="${1:?usage: $0 <region>   e.g. us-east-2 (prod) or us-west-1 (staging)}"
POOL="100.64.0.0/10"

# Meta x2p proxy bypass for AWS endpoints
export NO_PROXY="${NO_PROXY:-},.amazonaws.com"
export no_proxy="${no_proxy:-},.amazonaws.com"

# Match anything in 100.64.0.0/10 (100.64.x.x – 100.127.x.x) plus any 100.x supernet
CGNAT_RE='^100\.((6[4-9])|(7[0-9])|(8[0-9])|(9[0-9])|(1[01][0-9])|(12[0-7]))\.'
SUPERNET_RE='^100\.[0-9]+\.[0-9]+\.[0-9]+/([1-9]|10)$'

echo "=== CGNAT overlap audit for $POOL in $REGION (profile=${AWS_PROFILE:-default}) ==="

# 1) Transit Gateway route tables
echo "[1/3] Transit Gateway route tables…"
TGW_IDS=$(aws ec2 describe-transit-gateways --region "$REGION" \
  --filters Name=state,Values=available \
  --query 'TransitGateways[].TransitGatewayId' --output text 2>/dev/null || true)

if [[ -z "$TGW_IDS" ]]; then
  echo "  no TGWs in $REGION"
else
  for TGW in $TGW_IDS; do
    for RT in $(aws ec2 describe-transit-gateway-route-tables --region "$REGION" \
      --filters "Name=transit-gateway-id,Values=$TGW" \
      --query 'TransitGatewayRouteTables[].TransitGatewayRouteTableId' --output text); do
      SUB=$(aws ec2 search-transit-gateway-routes --region "$REGION" \
        --transit-gateway-route-table-id "$RT" \
        --filters "Name=route-search.subnet-of-match,Values=$POOL" \
        --query 'Routes[]' --output json 2>/dev/null || echo '[]')
      # shellcheck disable=SC2016  # backticks are JMESPath literal syntax, not shell expansion
      SUP=$(aws ec2 search-transit-gateway-routes --region "$REGION" \
        --transit-gateway-route-table-id "$RT" \
        --filters "Name=route-search.supernet-of-match,Values=$POOL" \
        --query 'Routes[?DestinationCidrBlock!=`0.0.0.0/0`]' --output json 2>/dev/null || echo '[]')
      [[ "$SUB" != "[]" || "$SUP" != "[]" ]] \
        && printf '  HIT: TGW=%s RT=%s subnet=%s supernet=%s\n' "$TGW" "$RT" "$SUB" "$SUP"
    done
  done
fi

# 2) VPC peering connections
echo "[2/3] VPC peering connections…"
aws ec2 describe-vpc-peering-connections --region "$REGION" \
  --filters Name=status-code,Values=active --output json \
  | jq -r --arg re "$CGNAT_RE" --arg sup "$SUPERNET_RE" '
  .VpcPeeringConnections[] | . as $p |
  (.RequesterVpcInfo.CidrBlockSet[]?.CidrBlock,
   .AccepterVpcInfo.CidrBlockSet[]?.CidrBlock) |
  select(test($re) or test($sup)) |
  "  HIT: peering=" + $p.VpcPeeringConnectionId + " cidr=" + .
'

# 3) OSDC VPC route tables (this account only)
echo "[3/3] VPC route tables…"
aws ec2 describe-route-tables --region "$REGION" --output json \
  | jq -r --arg re "$CGNAT_RE" --arg sup "$SUPERNET_RE" '
  .RouteTables[] | . as $rt | .Routes[] |
  select(.DestinationCidrBlock != null and
         (.DestinationCidrBlock | test($re) or test($sup))) |
  "  HIT: rt=" + $rt.RouteTableId + " vpc=" + $rt.VpcId +
  " dest=" + .DestinationCidrBlock +
  " target=" + (.GatewayId // .NatGatewayId // .TransitGatewayId //
                .VpcPeeringConnectionId // .NetworkInterfaceId // "unknown")
'

echo "=== done — pass criteria: zero HIT lines above ==="
