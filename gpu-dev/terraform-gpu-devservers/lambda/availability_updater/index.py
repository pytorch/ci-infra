"""
GPU Availability Updater Lambda
Updates GPU availability table when ASG instances launch/terminate
"""

import json
import logging
import os
import time
from typing import Dict, Any

import boto3

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
dynamodb = boto3.resource("dynamodb")
autoscaling = boto3.client("autoscaling")
ec2_client = boto3.client("ec2")

# Instance types per GPU type (for spot price lookups)
GPU_INSTANCE_TYPES = {
    "b300": "p6-b300.48xlarge", "b200": "p6-b200.48xlarge",
    "h200": "p5e.48xlarge", "h100": "p5.48xlarge", "a100": "p4d.24xlarge",
    "t4": "g4dn.12xlarge", "l4": "g6.12xlarge",
    "rtxpro6000": "g7e.24xlarge", "a10g": "g5.12xlarge",
    "cpu-x86": "c7i.8xlarge", "cpu-arm": "c7g.8xlarge",
}
SPOT_GPU_TYPES = os.environ.get("SPOT_GPU_TYPES", "")
# Keep an idle spot node warm this long after its last active reservation ended,
# so back-to-back reservations reuse it instead of paying a cold spot re-launch
# (which can also hit "no spot capacity"). Tracked via an ASG tag stamped each
# tick a reservation is active.
SPOT_KEEPALIVE_MINUTES = float(os.environ.get("SPOT_KEEPALIVE_MINUTES", "25"))
SPOT_LAST_ACTIVE_TAG = "gpu-dev/spot-last-active"


def get_spot_price_info(gpu_type: str) -> dict:
    """Query AWS for current spot price and derive availability signal."""
    instance_type = GPU_INSTANCE_TYPES.get(gpu_type)
    if not instance_type:
        return {}
    try:
        resp = ec2_client.describe_spot_price_history(
            InstanceTypes=[instance_type],
            ProductDescriptions=["Linux/UNIX"],
            MaxResults=5,
        )
        prices = resp.get("SpotPriceHistory", [])
        if not prices:
            return {"spot_price": None, "spot_available": False, "spot_signal": "No spot data"}
        latest = prices[0]
        price = float(latest["SpotPrice"])
        az = latest["AvailabilityZone"]
        ts = latest["Timestamp"].isoformat() if hasattr(latest["Timestamp"], "isoformat") else str(latest["Timestamp"])
        return {
            "spot_price": str(round(price, 2)),
            "spot_az": az,
            "spot_price_updated": ts,
            "spot_available": True,
            "spot_signal": f"${round(price, 2)}/hr in {az}",
        }
    except Exception as e:
        logger.warning(f"Spot price lookup failed for {gpu_type}: {e}")
        return {}

# Environment variables
AVAILABILITY_TABLE = os.environ["AVAILABILITY_TABLE"]
RESERVATIONS_TABLE = os.environ.get("RESERVATIONS_TABLE", "pytorch-gpu-dev-reservations")
SUPPORTED_GPU_TYPES = json.loads(os.environ["SUPPORTED_GPU_TYPES"])


def _parse_expires_at(value):
    """Parse the reservations table's `expires_at` field to a unix epoch (int).

    DDB stores it as either an ISO-8601 datetime string ("2026-05-02T00:12:03.674845") OR
    occasionally a numeric epoch — handle both. Returns None if unparseable.
    """
    if value is None:
        return None
    # Numeric (Decimal/int/float) → epoch seconds directly.
    if not isinstance(value, str):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None
    s = value.strip()
    if not s:
        return None
    # ISO-8601 first (the actual production format).
    try:
        from datetime import datetime, timezone
        # `fromisoformat` accepts microseconds; tolerate optional 'Z' suffix.
        if s.endswith("Z"):
            s2 = s[:-1] + "+00:00"
        else:
            s2 = s
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            # Convention in this codebase: timestamps written via datetime.utcnow().isoformat() are UTC.
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        pass
    # Numeric-as-string fallback.
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def get_gpu_resource_name(gpu_type: str) -> str:
    return SUPPORTED_GPU_TYPES.get(gpu_type, {}).get("k8s_resource", "nvidia.com/gpu")

def get_node_label_value(gpu_type: str) -> str:
    return SUPPORTED_GPU_TYPES.get(gpu_type, {}).get("node_gpu_type", gpu_type)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Handle ASG capacity change events - update all GPU types"""
    try:
        logger.info(f"Processing availability update event: {json.dumps(event)}")

        # Extract event details for logging
        detail = event.get("detail", {})
        event_type = event.get("detail-type", "")
        asg_name = detail.get("AutoScalingGroupName", "")
        instance_id = detail.get("EC2InstanceId", "")

        logger.info(f"Event: {event_type}, ASG: {asg_name}, Instance: {instance_id}")
        logger.info("Updating availability for ALL GPU types...")

        # Set up Kubernetes client once for all GPU types
        k8s_client = None
        try:
            logger.info("Setting up shared Kubernetes client for all GPU types")
            from shared import setup_kubernetes_client
            k8s_client = setup_kubernetes_client()
            logger.info("Shared Kubernetes client ready")
        except Exception as k8s_setup_error:
            logger.error(f"Failed to setup Kubernetes client: {k8s_setup_error}")
            k8s_client = None

        # Cache active reservations once for the whole invocation (used for per-size ETAs)
        try:
            active_reservations = scan_active_reservations()
            logger.info(f"Cached {len(active_reservations)} active reservations for ETA computation")
        except Exception as scan_err:
            logger.warning(f"Failed to scan reservations table for ETAs: {scan_err}")
            active_reservations = []

        # Update availability for ALL GPU types (use any ASG event as trigger to refresh all)
        updated_types = []
        for gpu_type in SUPPORTED_GPU_TYPES.keys():
            try:
                logger.info(f"=== Starting update for GPU type: {gpu_type} ===")
                update_gpu_availability(gpu_type, k8s_client, active_reservations=active_reservations)
                updated_types.append(gpu_type)
                logger.info(f"=== Successfully updated availability for GPU type: {gpu_type} ===")
            except Exception as gpu_error:
                logger.error(f"=== Failed to update availability for {gpu_type}: {gpu_error} ===")
                # Continue with other GPU types

        # Best-effort: delete stale rows for SKUs no longer in SUPPORTED_GPU_TYPES
        # (e.g. after a GPU type rename like g7e -> rtxpro6000).
        try:
            cleanup_stale_availability_rows()
        except Exception as cleanup_err:
            logger.warning(f"Stale-row cleanup failed: {cleanup_err}")

        # Scale down idle spot ASGs. This runs every ~5 min (EventBridge schedule)
        # and catches cases where: reservation cancelled/expired → no SQS messages
        # → reservation_processor's sweep-end scale-down never fires because SQS
        # has nothing to deliver.
        if SPOT_GPU_TYPES:
            spot_list = [t.strip() for t in SPOT_GPU_TYPES.split(",")] if SPOT_GPU_TYPES.strip() != "all" else list(SUPPORTED_GPU_TYPES.keys())
            for st in spot_list:
                try:
                    asg = f"{os.environ.get('ASG_NAME_PREFIX', 'pytorch-gpu-dev-gpu-nodes')}-{st}"
                    # Check if any active/queued/preparing reservations exist for this type
                    # gpu_type in DDB may be upper or lowercase, so check both
                    has_active = False
                    reservations_table = dynamodb.Table(os.environ.get("RESERVATIONS_TABLE", "pytorch-gpu-dev-reservations"))
                    for status in ["active", "preparing", "queued", "pending"]:
                        resp = reservations_table.query(
                            IndexName="StatusIndex",
                            KeyConditionExpression="#s = :status",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":status": status},
                        )
                        for item in resp.get("Items", []):
                            gt = (item.get("gpu_type") or "").lower()
                            if gt == st.lower():
                                has_active = True
                                break
                        if has_active:
                            break
                    if has_active:
                        # Stamp the ASG so we keep it warm for a grace period after
                        # this reservation ends (see SPOT_KEEPALIVE_MINUTES).
                        try:
                            autoscaling.create_or_update_tags(Tags=[{
                                "ResourceId": asg,
                                "ResourceType": "auto-scaling-group",
                                "Key": SPOT_LAST_ACTIVE_TAG,
                                "Value": str(int(time.time())),
                                "PropagateAtLaunch": False,
                            }])
                        except Exception as tag_err:
                            logger.warning(f"could not stamp {SPOT_LAST_ACTIVE_TAG} on {asg}: {tag_err}")
                    else:
                        resp = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg])
                        groups = resp.get("AutoScalingGroups", [])
                        if groups and groups[0]["DesiredCapacity"] > 0:
                            # Honor the keep-alive grace period: only scale to 0 once it's
                            # been idle SPOT_KEEPALIVE_MINUTES past the last active reservation.
                            last_active = 0
                            for tag in groups[0].get("Tags", []):
                                if tag.get("Key") == SPOT_LAST_ACTIVE_TAG:
                                    try:
                                        last_active = int(tag.get("Value") or 0)
                                    except (ValueError, TypeError):
                                        last_active = 0
                                    break
                            idle_for = time.time() - last_active if last_active else None
                            if idle_for is not None and idle_for < SPOT_KEEPALIVE_MINUTES * 60:
                                logger.info(
                                    f"Spot ASG {asg} idle {int(idle_for)}s < grace "
                                    f"{SPOT_KEEPALIVE_MINUTES}m — keeping warm")
                            else:
                                logger.info(f"Scaling down idle spot ASG {asg} to 0 (grace elapsed)")
                                autoscaling.set_desired_capacity(AutoScalingGroupName=asg, DesiredCapacity=0)
                        else:
                            logger.debug(f"Spot ASG {asg} already at 0 or not found")
                except Exception as sd_err:
                    logger.warning(f"Spot scale-down check for {st} failed: {sd_err}")

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Availability update completed",
                    "trigger_asg": asg_name,
                    "trigger_instance": instance_id,
                    "updated_gpu_types": updated_types,
                    "total_updated": len(updated_types),
                }
            ),
        }

    except Exception as e:
        logger.error(f"Error processing availability update: {str(e)}")
        raise


def update_gpu_availability(gpu_type: str, k8s_client=None, active_reservations=None) -> None:
    """Update availability information for a specific GPU type."""
    try:
        logger.info(f"Starting availability update for GPU type: {gpu_type}")

        # Get current ASG capacity - handle multiple ASGs per GPU type (e.g., capacity reservations)
        # MIG SKUs share the underlying h100 ASGs (cr-dedicated MIG node), so use the physical type for ASG matching
        asg_lookup_type = get_node_label_value(gpu_type)
        asg_name_prefix = f"pytorch-gpu-dev-gpu-nodes-{asg_lookup_type}"
        logger.info(f"Checking ASGs matching pattern: {asg_name_prefix}*")

        # Get all ASGs and filter by name pattern
        all_asgs_response = autoscaling.describe_auto_scaling_groups()
        matching_asgs = [
            asg for asg in all_asgs_response["AutoScalingGroups"]
            if asg["AutoScalingGroupName"].startswith(asg_name_prefix)
        ]

        if not matching_asgs:
            logger.warning(f"No ASGs found matching pattern: {asg_name_prefix}*")
            return

        asg_names = [asg["AutoScalingGroupName"] for asg in matching_asgs]
        logger.info(f"Found {len(matching_asgs)} ASGs: {asg_names}")

        # Calculate total availability metrics across all matching ASGs
        # For MIG SKUs we cannot tell from ASG alone which instances are MIG-partitioned;
        # we override running_instances later from k8s allocatable.
        is_mig_sku = "k8s_resource" in SUPPORTED_GPU_TYPES.get(gpu_type, {})
        desired_capacity = sum(asg["DesiredCapacity"] for asg in matching_asgs)
        running_instances = sum(
            len([
                instance for instance in asg["Instances"]
                if instance["LifecycleState"] == "InService"
            ]) for asg in matching_asgs
        )

        # Get GPU configuration for this type
        gpu_config = SUPPORTED_GPU_TYPES.get(gpu_type, {})
        gpus_per_instance = gpu_config.get("gpus_per_instance", 8)

        # Handle CPU-only nodes differently (they don't have GPUs)
        is_cpu_type = gpus_per_instance == 0

        if is_cpu_type:
            # For CPU nodes, report instance slots (assuming 3 users per node)
            max_users_per_node = 3
            total_gpus = running_instances * max_users_per_node
            logger.info(
                f"CPU ASG calculation: {running_instances} instances * {max_users_per_node} slots = {total_gpus} total slots")

            # Check actual pod usage on CPU nodes
            if k8s_client is not None:
                try:
                    logger.info(f"Checking CPU node availability for {gpu_type}")
                    # Count available slots by checking pod count on each node
                    v1 = client.CoreV1Api(k8s_client)
                    nodes = v1.list_node(label_selector=f"GpuType={get_node_label_value(gpu_type)}")

                    total_available_slots = 0
                    for node in nodes.items:
                        if is_node_ready_and_schedulable(node):
                            # Count gpu-dev pods on this node
                            pods = v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node.metadata.name}")
                            gpu_dev_pods = [p for p in pods.items if p.metadata.name.startswith('gpu-dev-')]
                            used_slots = len(gpu_dev_pods)
                            available_slots = max(0, max_users_per_node - used_slots)
                            total_available_slots += available_slots

                    available_gpus = total_available_slots
                    logger.info(f"Found {available_gpus} available CPU slots across {len(nodes.items)} nodes")
                except Exception as k8s_error:
                    logger.warning(f"Failed to query Kubernetes for {gpu_type} CPU availability: {k8s_error}")
                    available_gpus = total_gpus
            else:
                available_gpus = total_gpus
        else:
            # GPU nodes - use K8s schedulable node count for total if available
            total_gpus = running_instances * gpus_per_instance
            logger.info(
                f"ASG calculation: {running_instances} instances * {gpus_per_instance} GPUs = {total_gpus} total GPUs")

            # Query Kubernetes API for actual GPU allocations
            if k8s_client is not None:
                try:
                    logger.info(f"Starting Kubernetes query for {gpu_type} GPU availability")
                    available_gpus = check_schedulable_gpus_for_type(k8s_client, gpu_type)
                    logger.info(f"Kubernetes reports {available_gpus} schedulable {gpu_type.upper()} GPUs")

                except Exception as k8s_error:
                    logger.warning(f"Failed to query Kubernetes for {gpu_type} availability: {k8s_error}")
                    # Fallback to ASG-based calculation (assume all GPUs available)
                    available_gpus = total_gpus
            else:
                logger.warning(f"No Kubernetes client available for {gpu_type}, using ASG-based calculation")
                # Fallback to ASG-based calculation (assume all GPUs available)
                available_gpus = total_gpus

        # Calculate full nodes available (nodes with all GPUs free) and max reservable
        full_nodes_available = 0
        max_reservable = 0  # Maximum GPUs reservable (considering multinode for high-end GPUs)
        if k8s_client is not None and not is_cpu_type:
            try:
                from kubernetes import client as k8s_client_lib
                v1 = k8s_client_lib.CoreV1Api(k8s_client)
                node_label_value = get_node_label_value(gpu_type)
                resource_name = get_gpu_resource_name(gpu_type)
                nodes = v1.list_node(label_selector=f"GpuType={node_label_value}")

                single_node_max = 0  # Max available on any single node
                schedulable_total_gpus = 0  # Total GPUs on schedulable (non-cordoned) nodes
                full_node_gpu_counts = []  # Track actual GPU count per full node (accounts for MIG)
                for node in nodes.items:
                    if is_node_ready_and_schedulable(node):
                        available_on_node = get_available_gpus_on_node(v1, node, gpu_type)
                        total_on_node = 0
                        if node.status.allocatable:
                            gpu_allocatable = node.status.allocatable.get(resource_name, "0")
                            try:
                                total_on_node = int(gpu_allocatable)
                            except (ValueError, TypeError):
                                pass

                        schedulable_total_gpus += total_on_node

                        # Track max available on any single node
                        single_node_max = max(single_node_max, available_on_node)

                        # Count as full node if all GPUs are available
                        if total_on_node > 0 and available_on_node == total_on_node:
                            full_nodes_available += 1
                            full_node_gpu_counts.append(total_on_node)

                total_gpus = schedulable_total_gpus
                # For MIG SKUs override running_instances to the number of MIG-partitioned nodes
                if is_mig_sku:
                    running_instances = sum(1 for n in nodes.items if is_node_ready_and_schedulable(n) and int((n.status.allocatable or {}).get(resource_name, "0")) > 0)

                # Calculate max reservable using actual per-node GPU counts (not ASG gpus_per_instance)
                # This correctly accounts for MIG-configured nodes that have fewer full GPUs
                multinode_gpu_types = ['h100', 'h200', 'b200', 'a100']
                if gpu_type in multinode_gpu_types and full_node_gpu_counts:
                    # Sum the top N full nodes (up to 4 for multinode)
                    sorted_counts = sorted(full_node_gpu_counts, reverse=True)
                    max_reservable = sum(sorted_counts[:4])

                    if max_reservable == 0:
                        max_reservable = single_node_max
                else:
                    max_reservable = single_node_max

                logger.info(f"Found {full_nodes_available} full nodes available for {gpu_type}, max reservable: {max_reservable} (single node max: {single_node_max})")
            except Exception as e:
                logger.warning(f"Could not calculate full nodes available for {gpu_type}: {str(e)}")
                # Fallback: use available_gpus so max_reservable isn't misleadingly 0
                full_nodes_available = available_gpus // gpus_per_instance if gpus_per_instance > 0 else 0
                max_reservable = available_gpus
        elif is_cpu_type:
            # For CPU nodes, each node supports 1 reservation
            full_nodes_available = available_gpus  # Each "GPU" represents one CPU node slot
            max_reservable = 1 if available_gpus > 0 else 0  # Max 1 CPU node per reservation

        # Compute per-size ETAs (when each interesting reservation size first becomes reservable).
        size_etas: Dict[str, int] = {}
        if k8s_client is not None and not is_cpu_type and active_reservations is not None:
            try:
                from kubernetes import client as k8s_lib
                v1 = k8s_lib.CoreV1Api(k8s_client)
                size_etas = compute_size_etas(
                    v1=v1,
                    gpu_type=gpu_type,
                    node_label_value=get_node_label_value(gpu_type),
                    resource_name=get_gpu_resource_name(gpu_type),
                    gpus_per_instance=int(gpus_per_instance),
                    active_reservations=active_reservations,
                )
                logger.info(f"Computed size_etas for {gpu_type}: {size_etas}")
            except Exception as eta_err:
                logger.warning(f"Failed to compute size_etas for {gpu_type}: {eta_err}")
                size_etas = {}

        # Update DynamoDB table (update_item preserves maintenance fields set manually)
        table = dynamodb.Table(AVAILABILITY_TABLE)
        last_updated = context.aws_request_id if "context" in locals() else "unknown"
        last_updated_ts = int(time.time()) if "time" in dir() else 0

        # Fetch spot price info for spot-eligible types
        spot_info = {}
        if SPOT_GPU_TYPES and (SPOT_GPU_TYPES.strip() == "all" or gpu_type in [t.strip() for t in SPOT_GPU_TYPES.split(",")]):
            spot_info = get_spot_price_info(gpu_type)

        update_expr = (
            "SET total_gpus = :tg, available_gpus = :ag, max_reservable = :mr, "
            "full_nodes_available = :fn, running_instances = :ri, desired_capacity = :dc, "
            "gpus_per_instance = :gpi, last_updated = :lu, last_updated_timestamp = :lut, "
            "size_etas = :se"
        )
        expr_values = {
            ":tg": total_gpus,
            ":ag": available_gpus,
            ":mr": max_reservable,
            ":fn": full_nodes_available,
            ":ri": running_instances,
            ":dc": desired_capacity,
            ":gpi": gpus_per_instance,
            ":lu": last_updated,
            ":lut": last_updated_ts,
            ":se": size_etas,
        }
        if spot_info:
            update_expr += ", spot_info = :si"
            expr_values[":si"] = spot_info

        table.update_item(
            Key={"gpu_type": gpu_type},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )

        logger.info(
            f"Updated {gpu_type}: {available_gpus}/{total_gpus} GPUs available ({running_instances} instances, {full_nodes_available} full nodes, max reservable: {max_reservable})"
        )

    except Exception as e:
        logger.error(f"Error updating availability for {gpu_type}: {str(e)}")
        raise


import time


def check_schedulable_gpus_for_type(k8s_client, gpu_type: str) -> int:
    """Check how many GPUs of a specific type are schedulable (available for new pods)"""
    try:
        logger.info(f"Starting schedulable GPU check for type: {gpu_type}")
        from kubernetes import client

        v1 = client.CoreV1Api(k8s_client)
        logger.info(f"Created CoreV1Api client for {gpu_type}")

        # Get all nodes with the specified GPU type
        gpu_type_selector = f"GpuType={get_node_label_value(gpu_type)}"
        logger.info(f"Querying nodes with label selector: {gpu_type_selector}")

        nodes = v1.list_node(label_selector=gpu_type_selector)
        logger.info(f"Retrieved {len(nodes.items) if nodes.items else 0} nodes for {gpu_type}")

        if not nodes.items:
            logger.warning(f"No nodes found for GPU type {gpu_type}")
            return 0

        total_schedulable = 0

        for i, node in enumerate(nodes.items):
            logger.info(f"Processing node {i + 1}/{len(nodes.items)}: {node.metadata.name}")

            if not is_node_ready_and_schedulable(node):
                logger.info(f"Node {node.metadata.name} is not ready/schedulable, skipping")
                continue

            logger.info(f"Node {node.metadata.name} is ready, checking GPU availability")
            # Get available GPUs on this node
            available_on_node = get_available_gpus_on_node(v1, node, gpu_type)
            total_schedulable += available_on_node
            logger.info(f"Node {node.metadata.name}: {available_on_node} GPUs available")

        logger.info(f"Found {total_schedulable} schedulable {gpu_type.upper()} GPUs across {len(nodes.items)} nodes")
        return total_schedulable

    except Exception as e:
        logger.error(f"Error checking schedulable GPUs for type {gpu_type}: {str(e)}")
        return 0


def is_node_ready_and_schedulable(node) -> bool:
    """Check if a node is ready and schedulable"""
    try:
        # Check node conditions
        conditions = node.status.conditions or []
        is_ready = False

        for condition in conditions:
            if condition.type == "Ready":
                is_ready = condition.status == "True"
                break

        if not is_ready:
            return False

        # Check if node is schedulable (not cordoned)
        return not node.spec.unschedulable

    except Exception as e:
        logger.error(f"Error checking node readiness: {str(e)}")
        return False


def get_available_gpus_on_node(v1_api, node, gpu_type: str = None) -> int:
    """Get number of available GPUs (or MIG slices) on a specific node for the given SKU."""
    try:
        node_name = node.metadata.name
        resource_name = get_gpu_resource_name(gpu_type) if gpu_type else "nvidia.com/gpu"
        logger.info(f"Checking GPU availability on node: {node_name} (resource={resource_name})")

        # Get all pods on this node
        logger.info(f"Querying pods on node {node_name}")
        pods = v1_api.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}")
        logger.info(f"Found {len(pods.items)} pods on node {node_name}")

        # Calculate GPU usage
        used_gpus = 0
        for pod in pods.items:
            if pod.status.phase in ["Running", "Pending"]:
                # Warm-pool pods that are still 'ready' hold the slice but are
                # claimed instantly, so count them as available, not used.
                labels = pod.metadata.labels or {}
                if labels.get("app") == "gpu-dev-warm" and labels.get("warm-state") == "ready":
                    continue
                for container in pod.spec.containers:
                    if container.resources and container.resources.requests:
                        gpu_request = container.resources.requests.get(
                            resource_name, "0"
                        )
                        try:
                            used_gpus += int(gpu_request)
                        except (ValueError, TypeError):
                            pass

        # Get total GPUs on this node
        total_gpus = 0
        if node.status.allocatable:
            gpu_allocatable = node.status.allocatable.get(resource_name, "0")
            try:
                total_gpus = int(gpu_allocatable)
            except (ValueError, TypeError):
                pass

        available_gpus = max(0, total_gpus - used_gpus)
        logger.debug(f"Node {node_name}: {available_gpus}/{total_gpus} GPUs available")

        return available_gpus

    except Exception as e:
        logger.error(
            f"Error getting available GPUs on node {node.metadata.name}: {str(e)}"
        )
        return 0

def scan_active_reservations():
    """Return list of active reservation rows from the reservations DDB table.

    Each row is the raw DDB resource-style dict (keys + native types). Caller is
    responsible for tolerating Decimals and missing fields.
    """
    table = dynamodb.Table(RESERVATIONS_TABLE)
    items = []
    last_key = None
    while True:
        kwargs = {
            "FilterExpression": "#s = :s",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":s": "active"},
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return items


# Multinode-eligible types (mirrors the older multinode_gpu_types list elsewhere in this file).
_MULTINODE_TYPES = {"h100", "h200", "b200", "a100"}


def compute_size_etas(v1, gpu_type, node_label_value, resource_name, gpus_per_instance, active_reservations):
    """For each interesting reservation size, compute when it first becomes reservable.

    Returns a dict mapping the size (as a string) to a unix timestamp (int).
    A timestamp <= now means the size is currently available; sizes that won't
    fit in any foreseeable future (e.g. cluster too small) are omitted.
    """
    import time as _time
    now = int(_time.time())

    # 1) Get nodes and per-node capacity for this resource.
    try:
        nodes = v1.list_node(label_selector=f"GpuType={node_label_value}")
    except Exception as e:
        logger.warning(f"compute_size_etas: list_node failed: {e}")
        return {}

    node_state = {}  # node_name -> {capacity, used_now, expirations: [(ts, gpus)]}
    for node in nodes.items:
        if not is_node_ready_and_schedulable(node):
            continue
        capacity = 0
        try:
            capacity = int((node.status.allocatable or {}).get(resource_name, "0"))
        except (ValueError, TypeError):
            capacity = 0
        if capacity == 0:
            continue
        node_state[node.metadata.name] = {
            "capacity": capacity,
            "used_now": 0,
            "expirations": [],
        }

    if not node_state:
        return {}

    # 2) Map pods on these nodes to their gpu request and node.
    pod_to_info = {}  # pod_name -> (node_name, gpus_requested)
    try:
        pods = v1.list_namespaced_pod("gpu-dev")
    except Exception as e:
        logger.warning(f"compute_size_etas: list_pod failed: {e}")
        return {}
    for pod in pods.items:
        if not pod.spec or not pod.spec.node_name:
            continue
        if pod.spec.node_name not in node_state:
            continue
        if pod.status and pod.status.phase not in ("Running", "Pending"):
            continue
        gpus = 0
        if pod.spec.containers:
            for c in pod.spec.containers:
                if c.resources and c.resources.requests:
                    try:
                        gpus += int(c.resources.requests.get(resource_name, "0"))
                    except (ValueError, TypeError):
                        pass
        if gpus > 0:
            pod_to_info[pod.metadata.name] = (pod.spec.node_name, gpus)
            # used_now is the k8s ground-truth — count every running/pending pod, not just those
            # we can match to a reservation row. Otherwise pods without DDB rows look like free GPUs.
            node_state[pod.spec.node_name]["used_now"] += gpus

    # 3) Cross-reference active reservations to attach expiry timestamps to each known pod.
    #    Pods without a matching reservation row keep their GPUs marked as used_now but have no
    #    expiration → they're treated as "never expiring" by the simulation, which is the safe
    #    fallback (we don't fabricate ETAs for usage we can't trace).
    target_gpu_type_lower = gpu_type.lower()
    for r in active_reservations:
        # Reservations table stores gpu_type uppercased ("H100"); compare case-insensitively.
        rgt = r.get("gpu_type", "")
        if isinstance(rgt, str) and rgt.lower() != target_gpu_type_lower:
            continue
        pod_name = r.get("pod_name")
        expires_at = r.get("expires_at")
        if not pod_name or expires_at is None:
            continue
        if pod_name not in pod_to_info:
            continue
        ts = _parse_expires_at(expires_at)
        if ts is None:
            continue
        node_name, gpus = pod_to_info[pod_name]
        node_state[node_name]["expirations"].append((ts, gpus))

    # Sort each node's expirations by time.
    for ns in node_state.values():
        ns["expirations"].sort()

    def first_time_size_fits_single_node(size):
        """Earliest timestamp at which any single node has `size` GPUs free."""
        earliest = None
        for ns in node_state.values():
            free_now = ns["capacity"] - ns["used_now"]
            if free_now >= size:
                return now
            cum = free_now
            for ts, gpus in ns["expirations"]:
                cum += gpus
                if cum >= size:
                    if earliest is None or ts < earliest:
                        earliest = ts
                    break
        return earliest

    def first_time_k_full_nodes(k):
        """Earliest timestamp at which K nodes are simultaneously fully free."""
        free_at = []
        for ns in node_state.values():
            if ns["used_now"] == 0:
                free_at.append(now)
            elif ns["expirations"]:
                free_at.append(max(ts for ts, _ in ns["expirations"]))
        free_at.sort()
        if len(free_at) >= k:
            return free_at[k - 1]
        return None

    etas = {}
    # Single-node sizes 1, 2, 4, 8 (capped at the per-instance maximum).
    for size in (1, 2, 4, 8):
        if size > gpus_per_instance:
            break
        eta = first_time_size_fits_single_node(size)
        if eta is not None:
            etas[str(size)] = eta

    # Multinode sizes — only for SXM types with 8 GPUs per node.
    if gpus_per_instance == 8 and target_gpu_type_lower in _MULTINODE_TYPES:
        for k_nodes in (2, 3, 4, 5, 6):
            count = k_nodes * gpus_per_instance
            eta = first_time_k_full_nodes(k_nodes)
            if eta is not None:
                etas[str(count)] = eta

    return etas


def cleanup_stale_availability_rows():
    """Delete rows in the availability table whose gpu_type isn't in SUPPORTED_GPU_TYPES.

    Triggered on every Lambda invocation. Idempotent. Used to garbage-collect renamed
    SKUs (e.g. g7e -> rtxpro6000) that would otherwise linger as zero rows.
    """
    table = dynamodb.Table(AVAILABILITY_TABLE)
    valid_keys = set(SUPPORTED_GPU_TYPES.keys())
    last_key = None
    deleted = []
    while True:
        kwargs = {"ProjectionExpression": "gpu_type"}
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            gt = item.get("gpu_type")
            if gt and gt not in valid_keys:
                table.delete_item(Key={"gpu_type": gt})
                deleted.append(gt)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    if deleted:
        logger.info(f"Deleted {len(deleted)} stale availability rows: {deleted}")
