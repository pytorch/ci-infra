#!/usr/bin/env python3
"""List all B200 nodes and their active reservations."""
import subprocess, json, os
os.environ["AWS_PROFILE"] = "admin"

# Get all B200 nodes
result = subprocess.run(["aws", "ec2", "describe-instances", "--region", "us-east-2",
    "--filters", "Name=tag:GpuType,Values=b200", "Name=instance-state-name,Values=running",
    "--output", "json"], capture_output=True, text=True)
ec2 = json.loads(result.stdout)
nodes = {}
for r in ec2["Reservations"]:
    for i in r["Instances"]:
        tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
        nodes[i["PrivateIpAddress"]] = {
            "dns": i["PrivateDnsName"],
            "instance_id": i["InstanceId"],
            "cr": tags.get("CapacityReservation", "none"),
            "asg": tags.get("Name", ""),
            "pods": []
        }

# Get all active B200 reservations via AWS CLI
result = subprocess.run(["aws", "dynamodb", "scan", "--table-name", "pytorch-gpu-dev-reservations",
    "--filter-expression", "#s = :active AND gpu_type = :b200",
    "--expression-attribute-names", '{"#s": "status"}',
    "--expression-attribute-values", '{":active": {"S": "active"}, ":b200": {"S": "B200"}}',
    "--projection-expression", "reservation_id, user_id, gpu_count, pod_name, pod_ip, expires_at",
    "--profile", "admin", "--region", "us-east-2", "--output", "json"],
    capture_output=True, text=True)
raw = json.loads(result.stdout)
# Convert DynamoDB format to plain dicts
resp = {"Items": [{k: v.get("S", v.get("N", "")) for k, v in item.items()} for item in raw["Items"]]}

# Match pods to nodes by pod_ip subnet
unmatched = []
for item in resp["Items"]:
    pod_ip = str(item.get("pod_ip", ""))
    matched = False
    if pod_ip:
        # EKS pod IPs are from the node's ENI secondary IPs - same /24 subnet
        pod_prefix = pod_ip.rsplit(".", 1)[0]
        for node_ip, node in nodes.items():
            node_prefix = node_ip.rsplit(".", 1)[0]
            if pod_prefix == node_prefix:
                node["pods"].append(item)
                matched = True
                break
    if not matched:
        unmatched.append(item)

# Print
for node_ip, node in sorted(nodes.items()):
    gpu_used = sum(int(p.get("gpu_count", 0)) for p in node["pods"])
    print(f"\n{'='*90}")
    print(f"NODE: {node['dns']}")
    print(f"  Instance: {node['instance_id']}  IP: {node_ip}  CR: {node['cr']}")
    print(f"  ASG: {node['asg']}  GPUs used: {gpu_used}/8")
    if not node["pods"]:
        print("  (no active reservations)")
    for p in node["pods"]:
        print(f"  {str(p.get('pod_name','')):20s}  {str(p.get('user_id','')):25s}  {p.get('gpu_count','')} GPUs  expires {str(p.get('expires_at','?'))}")

if unmatched:
    print(f"\n{'='*90}")
    print("UNMATCHED (could not map to a node):")
    for p in unmatched:
        print(f"  {str(p.get('pod_name','')):20s}  {str(p.get('user_id','')):25s}  {p.get('gpu_count','')} GPUs  pod_ip={p.get('pod_ip','?')}  expires {str(p.get('expires_at','?'))}")
