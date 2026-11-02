#!/usr/bin/env python3
"""Delete Route53 records leaked by the old delete_dns_record (A-vs-CNAME mismatch).

Only touches `<sub>.<domain>` CNAMEs pointing at an ELB and their `_port.<sub>` TXT,
and only when no non-terminal reservation owns <sub>. Dry-run unless --apply.

    python cleanup_leaked_dns.py                 # show what would go
    python cleanup_leaked_dns.py --apply         # delete
"""
import argparse
from collections import defaultdict

import boto3

TERMINAL = {"expired", "cancelled", "failed"}


def live_subdomains(table_name: str, region: str) -> set:
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    kwargs = {"ProjectionExpression": "domain_name, #s", "ExpressionAttributeNames": {"#s": "status"}}
    live = set()
    while True:
        page = table.scan(**kwargs)
        live |= {i["domain_name"].lower() for i in page["Items"]
                 if i.get("domain_name") and i.get("status") not in TERMINAL}
        if "LastEvaluatedKey" not in page:
            return live
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def live_subdomains_all(table_name: str, regions: list) -> set:
    """Union across every region's reservations table, in case one shares this zone."""
    return set().union(*(live_subdomains(table_name, r) for r in regions))


def leaked_records(route53, zone_id: str, domain: str, live: set) -> list:
    suffix = f".{domain.rstrip('.')}.".lower()
    by_sub = defaultdict(list)
    for page in route53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone_id):
        for rrs in page["ResourceRecordSets"]:
            name = rrs["Name"].lower()
            if not name.endswith(suffix):
                continue
            sub = name[: -len(suffix)]
            if rrs["Type"] == "CNAME" and "." not in sub and \
                    rrs["ResourceRecords"][0]["Value"].endswith(".elb.amazonaws.com"):
                by_sub[sub].append(rrs)
            elif rrs["Type"] == "TXT" and sub.startswith("_port.") and "." not in sub[6:]:
                by_sub[sub[6:]].append(rrs)
    return [rrs for sub, recs in sorted(by_sub.items()) if sub not in live for rrs in recs]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--domain", default="devservers.io")
    p.add_argument("--regions", default="us-east-2,us-east-1,us-west-1",
                   help="reservation tables to treat as live (comma-separated)")
    p.add_argument("--table", default="pytorch-gpu-dev-reservations")
    p.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = p.parse_args()

    route53 = boto3.client("route53")
    zone = route53.list_hosted_zones_by_name(DNSName=args.domain, MaxItems="1")["HostedZones"][0]
    zone_id = zone["Id"].split("/")[-1]
    live = live_subdomains_all(args.table, args.regions.split(","))
    leaked = leaked_records(route53, zone_id, args.domain, live)

    print(f"zone {zone_id}: {zone['ResourceRecordSetCount']} record sets, "
          f"{len(live)} live reservation domains, {len(leaked)} leaked records")
    for rrs in leaked[:10]:
        print(f"  {rrs['Type']:5} {rrs['Name']}")
    if len(leaked) > 10:
        print(f"  ... and {len(leaked) - 10} more")
    if not args.apply:
        print("dry run — pass --apply to delete")
        return

    for i in range(0, len(leaked), 100):
        batch = leaked[i:i + 100]
        route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": r} for r in batch]})
        print(f"deleted {i + len(batch)}/{len(leaked)}")


if __name__ == "__main__":
    main()
