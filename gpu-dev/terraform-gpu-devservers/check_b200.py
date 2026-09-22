#!/usr/bin/env python3
import subprocess, json, os
os.environ["AWS_PROFILE"] = "admin"

result = subprocess.run(["aws", "dynamodb", "scan", "--table-name", "pytorch-gpu-dev-reservations",
    "--filter-expression", "#s = :active AND gpu_type = :b200",
    "--expression-attribute-names", '{"#s": "status"}',
    "--expression-attribute-values", '{":active": {"S": "active"}, ":b200": {"S": "B200"}}',
    "--projection-expression", "pod_name, user_id, gpu_count, expires_at",
    "--profile", "admin", "--region", "us-east-2", "--output", "json"],
    capture_output=True, text=True)
raw = json.loads(result.stdout)
items = [{k: v.get("S", v.get("N", "")) for k, v in item.items()} for item in raw["Items"]]
items.sort(key=lambda x: x.get("expires_at", ""))

if not items:
    print("No active B200 reservations")
else:
    for i in items:
        print(f"{i['pod_name']:20s} {i['user_id']:25s} {i['gpu_count']:>2s} GPUs  expires {i.get('expires_at', '?')}")
    print(f"\nTotal: {len(items)} reservations, {sum(int(i['gpu_count']) for i in items)} GPUs")
