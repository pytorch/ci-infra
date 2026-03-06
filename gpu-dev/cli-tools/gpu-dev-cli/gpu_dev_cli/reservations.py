"""Minimal reservation management for GPU Dev CLI"""

import json
import os
import select
import signal
import sys
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any, Union

from botocore.exceptions import ClientError
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from .config import Config
from .name_generator import sanitize_name
from . import __version__

console = Console()


def _make_vscode_link(pod_name: str) -> str:
    """Create a clickable vscode:// URL for opening remote SSH connection

    Args:
        pod_name: The SSH host name (e.g., gpu-dev-34f5f9e0)

    Returns:
        A vscode:// URL that opens VS Code with the remote SSH connection
    """
    # VS Code remote SSH URL format: vscode://vscode-remote/ssh-remote+<host>/path
    return f"vscode://vscode-remote/ssh-remote+{pod_name}/home/dev"


def _make_cursor_link(pod_name: str) -> str:
    """Create a clickable cursor:// URL for opening remote SSH connection in Cursor

    Args:
        pod_name: The SSH host name (e.g., gpu-dev-34f5f9e0)

    Returns:
        A cursor:// URL that opens Cursor with the remote SSH connection
    """
    # Based on VS Code remote SSH URL format: cursor://vscode-remote/ssh-remote+<host>/path
    return f"cursor://vscode-remote/ssh-remote+{pod_name}/home/dev"


def get_version() -> str:
    """Get CLI version for inclusion in SQS messages"""
    return __version__


def _add_agent_forwarding_to_ssh(ssh_command: str) -> str:
    """Add SSH agent forwarding (-A) flag to SSH command if not already present"""
    try:
        if not ssh_command or not ssh_command.startswith("ssh "):
            return ssh_command

        # Check if -A is already in the command
        if " -A" in ssh_command or ssh_command.endswith(" -A"):
            return ssh_command

        # Add -A flag after 'ssh'
        parts = ssh_command.split(" ", 1)
        if len(parts) == 2:
            return f"ssh -A {parts[1]}"
        else:
            return "ssh -A"

    except Exception:
        return ssh_command


def _extract_latest_pod_event(pod_events: str) -> str:
    """Extract the most relevant pod event for display - simplified since Lambda now provides formatted messages"""
    if not pod_events:
        return "Starting pod..."

    # Lambda now provides pre-formatted messages, so just return them
    # Handle multi-line messages by taking the first meaningful line
    lines = pod_events.split("\n")
    for line in lines:
        line = line.strip()
        if line and not line.startswith("Events:"):
            return line

    return "Starting pod..."


def _generate_vscode_command(ssh_command: str) -> Optional[str]:
    """Generate VS Code remote connection command from SSH command"""
    try:
        # Extract remote server from SSH command
        # Expected format: ssh dev@<hostname> or various formats with -o options
        if not ssh_command or not ssh_command.startswith("ssh "):
            return None

        # Parse SSH command to extract hostname
        parts = ssh_command.split()
        hostname = None

        for i, part in enumerate(parts):
            if "@" in part and not part.startswith("-"):
                # Extract just the hostname part (e.g., from dev@hostname.io)
                hostname = part.split("@")[1]
                break

        if not hostname:
            return None

        # Generate VS Code command with ProxyCommand and agent forwarding
        # VS Code will use the ssh command options we provide
        remote_server = f"dev@{hostname}"

        # Escape single quotes in the ProxyCommand for shell
        proxy_cmd = "gpu-dev-ssh-proxy %h %p"

        return (
            f"code --remote ssh-remote+{remote_server} "
            f"--ssh-option ForwardAgent=yes "
            f"--ssh-option ProxyCommand='{proxy_cmd}' "
            f"--ssh-option StrictHostKeyChecking=no "
            f"--ssh-option UserKnownHostsFile=/dev/null "
            f"/home/dev"
        )

    except Exception:
        return None


def _generate_cursor_command(ssh_command: str) -> Optional[str]:
    """Generate Cursor remote connection command from SSH command"""
    try:
        # Extract remote server from SSH command
        # Expected format: ssh dev@<hostname> or various formats with -o options
        if not ssh_command or not ssh_command.startswith("ssh "):
            return None

        # Parse SSH command to extract hostname
        parts = ssh_command.split()
        remote_server = parts[-1]
        if '@' in remote_server:
            remote_server = remote_server.split('@')[1]

        # Return the VS Code command format
        return f"cursor --remote ssh-remote+{remote_server} /home/dev"
    except Exception:
        return None


def _generate_ssh_config(hostname: str, pod_name: str) -> str:
    """Generate SSH config for a reservation

    Args:
        hostname: The FQDN hostname (e.g., old_bison.devservers.io)
        pod_name: The pod name to use as SSH host alias

    Returns:
        SSH config content as string
    """
    config_content = f"""Host {pod_name}
    HostName {hostname}
    User dev
    ForwardAgent yes
    ProxyCommand gpu-dev-ssh-proxy %h %p
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
"""
    return config_content


def _check_ssh_config_permission() -> bool:
    """Check if user has given permission to modify ~/.ssh/config and ~/.cursor/ssh_config

    Returns:
        True if permission granted or already set up, False otherwise
    """
    import click
    from pathlib import Path

    gpu_dev_dir = Path.home() / ".gpu-dev"
    permission_file = gpu_dev_dir / ".ssh-config-permission"

    # Check if already asked and answered
    if permission_file.exists():
        try:
            response = permission_file.read_text().strip()
            return response == "yes"
        except Exception:
            pass

    # Check if Include already exists in either ~/.ssh/config or ~/.cursor/ssh_config
    config_files = [
        Path.home() / ".ssh" / "config",
        Path.home() / ".cursor" / "ssh_config",
    ]

    for ssh_config in config_files:
        if ssh_config.exists():
            try:
                content = ssh_config.read_text()
                if "Include ~/.gpu-dev/" in content:
                    # Already set up, save permission
                    gpu_dev_dir.mkdir(mode=0o700, exist_ok=True)
                    permission_file.write_text("yes")
                    return True
            except Exception:
                pass

    # Ask user for permission
    console.print("\n[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/yellow]")
    console.print("[cyan]🔧 SSH Configuration Setup[/cyan]\n")
    console.print("To enable easy SSH access and VS Code/Cursor Remote connections,")
    console.print("we can add GPU dev server configs to your SSH config files.")
    console.print("\n[dim]This adds one line at the top of:[/dim]")
    console.print("[dim]  • ~/.ssh/config[/dim]")
    console.print("[dim]  • ~/.cursor/ssh_config[/dim]")
    console.print("[dim]Line added: Include ~/.gpu-dev/*-sshconfig[/dim]\n")
    console.print("[green]Benefits:[/green]")
    console.print("  • Simple commands: [green]ssh <pod-name>[/green]")
    console.print("  • VS Code Remote works: [green]code --remote ssh-remote+<pod-name>[/green]")
    console.print("  • Cursor Remote works: Open Remote SSH in Cursor")
    console.print("\n[dim]Without this, you'll need to use: [green]ssh -F ~/.gpu-dev/<id>-sshconfig <pod-name>[/green][/dim]")
    console.print("[yellow]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/yellow]\n")

    approved = click.confirm("Add Include directive to SSH config files?", default=True)

    # Save response
    gpu_dev_dir.mkdir(mode=0o700, exist_ok=True)
    permission_file.write_text("yes" if approved else "no")

    return approved


def _ensure_ssh_config_includes_devgpu() -> bool:
    """Ensure ~/.ssh/config and ~/.cursor/ssh_config include ~/.devgpu/* configs for VS Code/Cursor compatibility

    Returns:
        True if Include was added/exists, False if user declined
    """
    from pathlib import Path

    # Check permission first
    if not _check_ssh_config_permission():
        return False

    include_line = "Include ~/.gpu-dev/*-sshconfig\n"

    # List of config files to update: ~/.ssh/config and ~/.cursor/ssh_config
    config_files = [
        (Path.home() / ".ssh", "config"),
        (Path.home() / ".cursor", "ssh_config"),
    ]

    success = False
    for config_dir, config_name in config_files:
        try:
            # Create directory if it doesn't exist
            config_dir.mkdir(mode=0o700, exist_ok=True)

            config_file = config_dir / config_name

            # Read existing config or create empty
            if config_file.exists():
                content = config_file.read_text()
            else:
                content = ""

            # Check if Include already exists
            if "Include ~/.gpu-dev/" in content:
                success = True
                continue

            # Add Include at the top (must be first in SSH config)
            new_content = include_line + "\n" + content
            config_file.write_text(new_content)
            config_file.chmod(0o600)
            success = True
        except Exception:
            # If one fails, we still try the other
            pass

    return success


def create_ssh_config_for_reservation(hostname: str, pod_name: str, reservation_id: str, name: Optional[str] = None) -> tuple[Optional[str], bool]:
    """Create SSH config file for a reservation in ~/.gpu-dev/

    Args:
        hostname: The FQDN hostname (e.g., old_bison.devservers.io)
        pod_name: The pod name to use as SSH host alias
        reservation_id: The reservation ID (full or short)
        name: Optional reservation name to use for filename (falls back to short ID)

    Returns:
        Tuple of (config_path, use_include) where:
          - config_path: Path to the created config file, or None on error
          - use_include: True if ~/.ssh/config includes devgpu configs, False if need -F flag
    """
    from pathlib import Path

    # Create ~/.gpu-dev directory
    gpu_dev_dir = Path.home() / ".gpu-dev"
    gpu_dev_dir.mkdir(mode=0o700, exist_ok=True)

    # Use short ID for filename (always safe, avoids issues with special chars like / in names)
    # For multinode, names like "16x B200 multinode - Node 1/2" contain / which breaks filenames
    short_id = reservation_id[:8]
    filename = f"{short_id}-sshconfig"

    config_file = gpu_dev_dir / filename
    config_content = _generate_ssh_config(hostname, pod_name)

    try:
        config_file.write_text(config_content)
        config_file.chmod(0o600)

        # Check/ask permission to include in ~/.ssh/config
        use_include = _ensure_ssh_config_includes_devgpu()

        return (str(config_file), use_include)
    except Exception:
        return (None, False)


def remove_ssh_config_for_reservation(reservation_id: str, name: Optional[str] = None) -> bool:
    """Remove SSH config file for a reservation

    Args:
        reservation_id: The reservation ID (full or short)
        name: Optional reservation name to use for filename (falls back to short ID, name param kept for backwards compat)

    Returns:
        True if successful (or file didn't exist), False on error
    """
    from pathlib import Path

    # Always use short ID for filename (consistent with create_ssh_config_for_reservation)
    short_id = reservation_id[:8]
    filename = f"{short_id}-sshconfig"

    config_file = Path.home() / ".gpu-dev" / filename

    try:
        if config_file.exists():
            config_file.unlink()
        return True
    except Exception:
        return False


def is_ssh_include_enabled() -> bool:
    """Check if user has approved SSH config Include directive

    Returns:
        True if Include is enabled, False otherwise
    """
    from pathlib import Path

    permission_file = Path.home() / ".gpu-dev" / ".ssh-config-permission"
    if permission_file.exists():
        try:
            return permission_file.read_text().strip() == "yes"
        except Exception:
            pass
    return False


def get_ssh_config_path(reservation_id: str, name: Optional[str] = None) -> str:
    """Get the SSH config file path for a reservation

    Args:
        reservation_id: The reservation ID (full or short)
        name: Optional reservation name to use for filename (falls back to short ID, name param kept for backwards compat)

    Returns:
        Path to the config file (may not exist)
    """
    from pathlib import Path
    # Always use short ID for filename (consistent with create_ssh_config_for_reservation)
    short_id = reservation_id[:8]
    filename = f"{short_id}-sshconfig"
    return str(Path.home() / ".gpu-dev" / filename)


class ReservationManager:
    """Minimal GPU reservations manager - AWS-only"""

    def __init__(self, config: Config):
        self.config = config
        self.reservations_table = config.dynamodb.Table(
            config.reservations_table)

    def create_reservation(
        self,
        user_id: str,
        gpu_count: int,
        gpu_type: str,
        duration_hours: Union[int, float],
        name: Optional[str] = None,
        github_user: Optional[str] = None,
        jupyter_enabled: bool = False,
        recreate_env: bool = False,
        dockerfile: Optional[str] = None,
        no_persistent_disk: bool = False,
        dockerimage: Optional[str] = None,
        preserve_entrypoint: bool = False,
        disk_name: Optional[str] = None,
        node_labels: Optional[Dict[str, str]] = None,
        trace: bool = False,
    ) -> Optional[str]:
        """Create a new GPU reservation"""
        try:
            import time
            trace_start = time.time() if trace else None

            reservation_id = str(uuid.uuid4())
            created_at = datetime.utcnow().isoformat()

            # Process the name: sanitize user input or let Lambda generate
            processed_name = None
            if name:
                # Sanitize user-provided name
                processed_name = sanitize_name(name)
                # If sanitization results in empty string, let Lambda generate
                if not processed_name:
                    processed_name = None
            # If no name provided, let Lambda generate (processed_name stays None)

            # Create initial reservation record for polling
            # Convert float to Decimal for DynamoDB compatibility
            duration_decimal = Decimal(str(duration_hours))

            initial_reservation = {
                "reservation_id": reservation_id,
                "user_id": user_id,
                "gpu_count": gpu_count,
                "gpu_type": gpu_type,
                "duration_hours": duration_decimal,
                "name": processed_name,
                "created_at": created_at,
                "status": "pending",
                "expires_at": (
                    datetime.utcnow() + timedelta(hours=duration_hours)
                ).isoformat(),
                "jupyter_enabled": jupyter_enabled,
            }

            # Add github_user if provided
            if github_user:
                initial_reservation["github_user"] = github_user

            # Send processing request to SQS queue (Lambda will create the initial record)
            # Use float for SQS message (JSON serializable)
            message = {
                "reservation_id": reservation_id,
                "user_id": user_id,
                "gpu_count": gpu_count,
                "gpu_type": gpu_type,
                "duration_hours": float(duration_hours),
                "name": processed_name,
                "created_at": created_at,
                "status": "pending",
                "jupyter_enabled": jupyter_enabled,
                "recreate_env": recreate_env,
                "no_persistent_disk": no_persistent_disk,
                "version": get_version(),
            }

            # Add github_user if provided
            if github_user:
                message["github_user"] = github_user

            # Add Docker options if provided
            if dockerfile:
                message["dockerfile"] = dockerfile
            if dockerimage:
                message["dockerimage"] = dockerimage
            # Always include preserve_entrypoint flag (don't make it conditional)
            message["preserve_entrypoint"] = preserve_entrypoint

            # Add disk_name if provided
            if disk_name:
                message["disk_name"] = disk_name

            # Add node_labels if provided (for node selection preferences)
            if node_labels:
                message["node_labels"] = node_labels

            # Add trace flag and CLI start timestamp
            if trace:
                message["trace"] = True
                message["trace_cli_start"] = trace_start

            queue_url = self.config.get_queue_url()
            sqs_send_start = time.time() if trace else None
            self.config.sqs_client.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(message)
            )
            if trace:
                sqs_send_duration = time.time() - sqs_send_start
                console.print(f"[dim]Trace: SQS send_message took {sqs_send_duration:.3f}s[/dim]")

            return reservation_id

        except Exception as e:
            console.print(f"[red]❌ Error creating reservation: {str(e)}[/red]")
            return None

    def create_multinode_reservation(
        self,
        user_id: str,
        gpu_count: int,
        gpu_type: str,
        duration_hours: Union[int, float],
        name: Optional[str] = None,
        github_user: Optional[str] = None,
        jupyter_enabled: bool = False,
        recreate_env: bool = False,
        dockerfile: Optional[str] = None,
        dockerimage: Optional[str] = None,
        no_persistent_disk: bool = False,
        preserve_entrypoint: bool = False,
        disk_name: Optional[str] = None,
        node_labels: Optional[Dict[str, str]] = None,
    ) -> Optional[List[str]]:
        """Create multiple GPU reservations for multinode setup"""
        try:
            # Determine GPU config
            gpu_configs = {
                "t4": {"max_gpus": 4},
                "l4": {"max_gpus": 4},
                "a10g": {"max_gpus": 4},
                "t4-small": {"max_gpus": 1},
                "g5g": {"max_gpus": 2},
                "a100": {"max_gpus": 8},
                "h100": {"max_gpus": 8},
                "h200": {"max_gpus": 8},
                "b200": {"max_gpus": 8},
            }

            max_gpus_per_node = gpu_configs[gpu_type]["max_gpus"]
            num_nodes = gpu_count // max_gpus_per_node

            if gpu_count % max_gpus_per_node != 0:
                console.print(
                    f"[red]❌ GPU count must be multiple of {max_gpus_per_node} for {gpu_type}[/red]")
                return None

            # Generate a master reservation ID to group related nodes
            master_reservation_id = str(uuid.uuid4())
            created_at = datetime.utcnow().isoformat()
            reservation_ids = []

            # Create reservation for each node
            for node_idx in range(num_nodes):
                node_reservation_id = str(uuid.uuid4())
                reservation_ids.append(node_reservation_id)

                # Node-specific name
                base_name = name or f'{gpu_count}x {gpu_type.upper()} multinode'
                node_name = f"{base_name} - Node {node_idx + 1}/{num_nodes}"

                # Create reservation message for this node
                message = {
                    "reservation_id": node_reservation_id,
                    "master_reservation_id": master_reservation_id,  # Group related nodes
                    "node_index": node_idx,
                    "total_nodes": num_nodes,
                    "user_id": user_id,
                    "gpu_count": max_gpus_per_node,  # GPUs per node
                    "total_gpu_count": gpu_count,  # Total GPUs across all nodes
                    "gpu_type": gpu_type,
                    "duration_hours": float(duration_hours),
                    "name": node_name,
                    "version": get_version(),
                    "created_at": created_at,
                    "status": "pending",
                    # Only enable Jupyter on master node
                    "jupyter_enabled": jupyter_enabled and node_idx == 0,
                    "recreate_env": recreate_env,
                    "is_multinode": True,
                    "no_persistent_disk": no_persistent_disk,
                }

                if github_user:
                    message["github_user"] = github_user

                # Add Docker options if provided
                if dockerfile:
                    message["dockerfile"] = dockerfile
                if dockerimage:
                    message["dockerimage"] = dockerimage
                # Always include preserve_entrypoint flag (don't make it conditional)
                message["preserve_entrypoint"] = preserve_entrypoint

                # Add disk_name if provided (only for master node in multinode setup)
                if disk_name and node_idx == 0:
                    message["disk_name"] = disk_name

                # Add node_labels if provided (for node selection preferences)
                if node_labels:
                    message["node_labels"] = node_labels

                # Send to SQS queue
                queue_url = self.config.get_queue_url()
                self.config.sqs_client.send_message(
                    QueueUrl=queue_url, MessageBody=json.dumps(message)
                )

            return reservation_ids

        except Exception as e:
            console.print(
                f"[red]❌ Error creating multinode reservation: {str(e)}[/red]")
            return None

    def list_reservations(
        self,
        user_filter: Optional[str] = None,
        statuses_to_include: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """List GPU reservations with flexible filtering"""
        try:
            all_reservations = []

            if user_filter:
                # Query by specific user with pagination
                response = self.reservations_table.query(
                    IndexName="UserIndex",
                    KeyConditionExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_filter},
                )
                all_reservations = response.get("Items", [])

                # Handle pagination for UserIndex query
                while "LastEvaluatedKey" in response:
                    response = self.reservations_table.query(
                        IndexName="UserIndex",
                        KeyConditionExpression="user_id = :user_id",
                        ExpressionAttributeValues={":user_id": user_filter},
                        ExclusiveStartKey=response["LastEvaluatedKey"]
                    )
                    all_reservations.extend(response.get("Items", []))
            else:
                # Query by status using StatusIndex (much faster than full scan)
                query_statuses = statuses_to_include or [
                    "active", "preparing", "queued", "pending",
                    "failed", "cancelled", "expired",
                ]
                for s in query_statuses:
                    response = self.reservations_table.query(
                        IndexName="StatusIndex",
                        KeyConditionExpression="#s = :status",
                        ExpressionAttributeNames={"#s": "status"},
                        ExpressionAttributeValues={":status": s},
                    )
                    all_reservations.extend(response.get("Items", []))
                    while "LastEvaluatedKey" in response:
                        response = self.reservations_table.query(
                            IndexName="StatusIndex",
                            KeyConditionExpression="#s = :status",
                            ExpressionAttributeNames={"#s": "status"},
                            ExpressionAttributeValues={":status": s},
                            ExclusiveStartKey=response["LastEvaluatedKey"],
                        )
                        all_reservations.extend(response.get("Items", []))

            # Filter by status if specified (needed for user_filter path;
            # the else path already queries only requested statuses)
            if statuses_to_include and user_filter:
                all_reservations = [
                    r for r in all_reservations
                    if r.get("status") in statuses_to_include
                ]

            return all_reservations

        except Exception as e:
            console.print(f"[red]❌ Error listing reservations: {str(e)}[/red]")
            return []

    def cancel_reservation(self, reservation_id: str, user_id: str) -> bool:
        """Cancel a GPU reservation by sending cancellation message to queue"""
        try:
            # Send cancellation request to SQS queue for processing
            message = {
                "type": "cancellation",
                "reservation_id": reservation_id,
                "user_id": user_id,
                "requested_at": datetime.utcnow().isoformat(),
                "version": get_version(),
            }

            queue_url = self.config.get_queue_url()
            self.config.sqs_client.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(message)
            )

            console.print(
                f"[yellow]⏳ Cancellation request submitted for reservation {reservation_id[:8]}...[/yellow]"
            )
            console.print(
                "[yellow]💡 The reservation will be cancelled shortly. Use 'gpu-dev list' to check status.[/yellow]"
            )
            return True

        except Exception as e:
            console.print(
                f"[red]❌ Error submitting cancellation request: {str(e)}[/red]"
            )
            return False

    def wait_for_multinode_reservation_completion(
        self, reservation_ids: List[str], timeout_minutes: Optional[int] = 10, verbose: bool = False
    ) -> Optional[List[Dict[str, Any]]]:
        """Poll for multiple reservation completion using shared polling logic"""
        return self._wait_for_reservations_completion(reservation_ids, timeout_minutes, is_multinode=True, verbose=verbose)

    def get_connection_info(
        self, reservation_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get SSH connection information for a reservation

        For multi-node reservations, returns info for all nodes in the group.
        """
        try:
            # Query by user first (efficient), then filter by reservation_id prefix
            response = self.reservations_table.query(
                IndexName="UserIndex",
                KeyConditionExpression="user_id = :user_id",
                ExpressionAttributeValues={":user_id": user_id},
            )
            all_reservations = response.get("Items", [])

            # Handle pagination for UserIndex query
            while "LastEvaluatedKey" in response:
                response = self.reservations_table.query(
                    IndexName="UserIndex",
                    KeyConditionExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_id},
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                all_reservations.extend(response.get("Items", []))

            # Filter by reservation_id prefix in memory
            matching_reservations = [
                res for res in all_reservations
                if res.get("reservation_id", "").startswith(reservation_id)
            ]

            if len(matching_reservations) == 0:
                return None
            elif len(matching_reservations) > 1:
                return None  # Ambiguous - need longer prefix

            reservation = matching_reservations[0]

            # Check if this is part of a multi-node group
            is_multinode = reservation.get("is_multinode", False)
            master_reservation_id = reservation.get("master_reservation_id")

            # Build base connection info
            connection_info = {
                "ssh_command": reservation.get("ssh_command", "ssh user@pending"),
                "pod_name": reservation.get("pod_name", "pending"),
                "namespace": reservation.get("namespace", "default"),
                "gpu_count": reservation["gpu_count"],
                "status": reservation["status"],
                "launched_at": reservation.get("launched_at"),
                "expires_at": reservation.get("expires_at"),
                "created_at": reservation.get("created_at"),
                "reservation_id": reservation["reservation_id"],
                "name": reservation.get("name"),
                "instance_type": reservation.get("instance_type", "unknown"),
                "gpu_type": reservation.get("gpu_type", "unknown"),
                "failure_reason": reservation.get("failure_reason", ""),
                "current_detailed_status": reservation.get("current_detailed_status", ""),
                "status_history": reservation.get("status_history", []),
                "pod_logs": reservation.get("pod_logs", ""),
                "jupyter_url": reservation.get("jupyter_url", ""),
                "jupyter_port": reservation.get("jupyter_port", ""),
                "jupyter_token": reservation.get("jupyter_token", ""),
                "jupyter_enabled": reservation.get("jupyter_enabled", False),
                "jupyter_error": reservation.get("jupyter_error", ""),
                "ebs_volume_id": reservation.get("ebs_volume_id", ""),
                "secondary_users": reservation.get("secondary_users", []),
                "warning": reservation.get("warning", ""),
                "is_multinode": is_multinode,
                "fqdn": reservation.get("fqdn", ""),
            }

            # If multi-node, fetch all nodes in the group
            if is_multinode and master_reservation_id:
                # Find all reservations with the same master_reservation_id
                multinode_reservations = [
                    res for res in all_reservations
                    if res.get("master_reservation_id") == master_reservation_id
                ]

                # Sort by node_index
                multinode_reservations.sort(key=lambda r: r.get("node_index", 0))

                # Add multi-node specific info
                connection_info["total_nodes"] = len(multinode_reservations)
                connection_info["nodes"] = []

                for node_res in multinode_reservations:
                    node_info = {
                        "reservation_id": node_res.get("reservation_id"),
                        "pod_name": node_res.get("pod_name"),
                        "ssh_command": node_res.get("ssh_command", "ssh user@pending"),
                        "node_index": node_res.get("node_index", 0),
                        "status": node_res.get("status"),
                        "name": node_res.get("name"),
                        "fqdn": node_res.get("fqdn"),
                    }
                    connection_info["nodes"].append(node_info)

            return connection_info

        except Exception as e:
            console.print(
                f"[red]❌ Error getting connection info: {str(e)}[/red]")
            return None

    def enable_jupyter(self, reservation_id: str, user_id: str) -> bool:
        """Enable Jupyter Lab for an active reservation"""
        try:
            # Send message to Lambda to start Jupyter service in pod
            # Lambda will handle both the pod changes and DynamoDB updates
            message = {
                "action": "enable_jupyter",
                "reservation_id": reservation_id,
                "user_id": user_id,
                "version": get_version(),
            }

            queue_url = self.config.get_queue_url()
            self.config.sqs_client.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(message)
            )

            console.print(
                f"[yellow]⏳ Jupyter enable request submitted for reservation {reservation_id[:8]}...[/yellow]"
            )

            # Poll for 3 minutes to show the outcome
            return self._poll_jupyter_action_result(
                reservation_id, user_id, "enable", timeout_minutes=3
            )

        except Exception as e:
            console.print(
                f"[red]❌ Error submitting Jupyter enable request: {str(e)}[/red]"
            )
            return False

    def disable_jupyter(self, reservation_id: str, user_id: str) -> bool:
        """Disable Jupyter Lab for an active reservation"""
        try:
            # Send message to Lambda to stop Jupyter service in pod
            # Lambda will handle both the pod changes and DynamoDB updates
            message = {
                "action": "disable_jupyter",
                "reservation_id": reservation_id,
                "user_id": user_id,
                "version": get_version(),
            }

            queue_url = self.config.get_queue_url()
            self.config.sqs_client.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(message)
            )

            console.print(
                f"[yellow]⏳ Jupyter disable request submitted for reservation {reservation_id[:8]}...[/yellow]"
            )

            # Poll for 3 minutes to show the outcome
            return self._poll_jupyter_action_result(
                reservation_id, user_id, "disable", timeout_minutes=3
            )

        except Exception as e:
            console.print(
                f"[red]❌ Error submitting Jupyter disable request: {str(e)}[/red]"
            )
            return False

    def add_user(self, reservation_id: str, user_id: str, github_username: str) -> bool:
        """Add a secondary user to an active reservation"""
        try:
            # Validate GitHub username format (basic validation)
            if (
                not github_username
                or not github_username.replace("-", "").replace("_", "").isalnum()
            ):
                console.print(
                    f"[red]❌ Invalid GitHub username: {github_username}[/red]"
                )
                return False

            # Send message to Lambda to add user SSH keys to pod
            # Lambda will handle fetching GitHub keys and updating the pod
            message = {
                "action": "add_user",
                "reservation_id": reservation_id,
                "user_id": user_id,
                "github_username": github_username,
                "version": get_version(),
            }

            queue_url = self.config.get_queue_url()
            self.config.sqs_client.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(message)
            )

            console.print(
                f"[yellow]⏳ Adding user {github_username} to reservation {reservation_id[:8]}...[/yellow]"
            )

            # Poll for 3 minutes to show the outcome
            return self._poll_add_user_result(
                reservation_id, user_id, github_username, timeout_minutes=3
            )

        except Exception as e:
            console.print(
                f"[red]❌ Error adding user {github_username}: {str(e)}[/red]"
            )
            return False

    def extend_reservation(self, reservation_id: str, user_id: str, extension_hours: float) -> bool:
        """Extend an active reservation by the specified number of hours"""
        try:
            # Capture current expiration BEFORE sending extension request to avoid race condition
            response = self.reservations_table.query(
                IndexName="UserIndex",
                KeyConditionExpression="user_id = :user_id",
                ExpressionAttributeValues={":user_id": user_id},
            )
            all_reservations = response.get("Items", [])

            # Handle pagination for UserIndex query
            while "LastEvaluatedKey" in response:
                response = self.reservations_table.query(
                    IndexName="UserIndex",
                    KeyConditionExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_id},
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                all_reservations.extend(response.get("Items", []))

            matching_reservations = [
                res for res in all_reservations
                if res.get("reservation_id", "").startswith(reservation_id)
            ]

            initial_expires_at = None
            full_reservation_id = reservation_id
            if matching_reservations:
                initial_expires_at = matching_reservations[0].get("expires_at", "")
                full_reservation_id = matching_reservations[0].get("reservation_id", reservation_id)

            # Send message to Lambda to extend reservation
            # Lambda will handle both the expiration timestamp update and any necessary pod updates
            message = {
                "action": "extend_reservation",
                "reservation_id": full_reservation_id,
                "user_id": user_id,
                "extension_hours": extension_hours,
                "version": get_version(),
            }

            queue_url = self.config.get_queue_url()
            self.config.sqs_client.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(message)
            )

            console.print(
                f"[yellow]⏳ Extension request submitted for reservation {reservation_id[:8]}...[/yellow]"
            )

            # Poll for 3 minutes to show the outcome
            return self._poll_extend_action_result(
                reservation_id, user_id, extension_hours, timeout_minutes=3, initial_expires_at=initial_expires_at
            )

        except Exception as e:
            console.print(
                f"[red]❌ Error submitting extension request: {str(e)}[/red]")
            return False

    def get_gpu_availability_by_type(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Get GPU availability information by GPU type from real-time availability table"""
        try:
            # Try to get real-time availability from the availability table
            availability_table_name = self.config.availability_table
            availability_table = self.config.dynamodb.Table(
                availability_table_name)

            # Scan the whole availability table with pagination
            response = availability_table.scan()
            availability_info = {}
            all_items = response.get("Items", [])

            # Handle pagination for availability table
            while "LastEvaluatedKey" in response:
                response = availability_table.scan(
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                all_items.extend(response.get("Items", []))

            for item in all_items:
                gpu_type = item["gpu_type"]
                queue_length = self._get_queue_length_for_gpu_type(gpu_type)
                estimated_wait = queue_length * 15 if queue_length > 0 else 0

                availability_info[gpu_type] = {
                    "available": int(item.get("available_gpus", 0)),
                    "total": int(item.get("total_gpus", 0)),
                    "max_reservable": int(item.get("max_reservable", 0)),
                    "full_nodes_available": int(item.get("full_nodes_available", 0)),
                    "gpus_per_instance": int(item.get("gpus_per_instance", 0)),
                    "queue_length": queue_length,
                    "estimated_wait_minutes": estimated_wait,
                    "running_instances": int(item.get("running_instances", 0)),
                    "desired_capacity": int(item.get("desired_capacity", 0)),
                    "last_updated": item.get("last_updated_timestamp", 0),
                }

            return availability_info

        except Exception as e:
            console.print(
                f"[red]❌ Error getting GPU availability: {str(e)}[/red]")
            return None

    def display_reservation_trace(self, reservation_id: str) -> None:
        """Display timing trace for a reservation"""
        try:
            from rich.table import Table

            # Get reservation from DynamoDB
            table = self.config.dynamodb.Table(self.config.reservations_table)
            response = table.get_item(Key={"reservation_id": reservation_id})

            if "Item" not in response:
                console.print(f"[red]Could not fetch trace for {reservation_id}[/red]")
                return

            reservation = response["Item"]
            trace_data = reservation.get("trace_data", {})

            if not trace_data:
                console.print("[yellow]No trace data available (trace flag not enabled)[/yellow]")
                return

            # Build timing table
            table = Table(title=f"Reservation Timing Trace ({reservation_id[:8]}...)")
            table.add_column("Step", style="cyan", no_wrap=True)
            table.add_column("Duration", style="green", justify="right")
            table.add_column("Cumulative", style="blue", justify="right")
            table.add_column("Details", style="dim")

            # Sort trace events by timestamp
            # Handle both Decimal (from DynamoDB) and float/int types
            from decimal import Decimal
            events = []
            for key, value in trace_data.items():
                if isinstance(value, (int, float, Decimal)):
                    events.append((key, float(value)))

            events.sort(key=lambda x: x[1])

            # Calculate durations
            cumulative = 0
            prev_time = None
            for i, (step, timestamp) in enumerate(events):
                if prev_time is not None:
                    duration = timestamp - prev_time
                    cumulative += duration
                    table.add_row(
                        step,
                        f"{duration:.3f}s",
                        f"{cumulative:.3f}s",
                        ""
                    )
                else:
                    table.add_row(step, "0.000s", "0.000s", "Start")
                prev_time = timestamp

            # Total duration
            if events:
                total_duration = events[-1][1] - events[0][1]
                table.add_row("", "", "", "")
                table.add_row("[bold]TOTAL[/bold]", f"[bold]{total_duration:.3f}s[/bold]", "", "")

            console.print("\n")
            console.print(table)
            console.print("\n")

        except Exception as e:
            console.print(f"[red]Error displaying trace: {str(e)}[/red]")

    def _get_static_gpu_config(
        self, gpu_type: str, queue_length: int, estimated_wait: int
    ) -> Dict[str, Any]:
        """Get static GPU configuration as fallback when real-time data unavailable"""
        static_configs = {
            # 2x p4d.24xlarge = 16 A100s
            "a100": {"available": 0, "total": 16},
            # 1x p6-b200.48xlarge = 8 B200s
            "b200": {"available": 0, "total": 8},
            # 2x p5e.48xlarge = 16 H200s
            "h200": {"available": 0, "total": 16},
            "h100": {"available": 0, "total": 16},  # 2x p5.48xlarge = 16 H100s
            "t4": {"available": 0, "total": 8},  # 2x g4dn.12xlarge = 8 T4s
        }

        config = static_configs.get(gpu_type, {"available": 0, "total": 0})
        return {
            "available": config["available"],
            "total": config["total"],
            "queue_length": queue_length,
            "estimated_wait_minutes": estimated_wait,
            "running_instances": 0,
            "desired_capacity": 0,
            "last_updated": 0,
        }

    def _get_queue_length_for_gpu_type(self, gpu_type: str) -> int:
        """Get the number of queued reservations for a specific GPU type"""
        try:
            total_count = 0

            # Count queued reservations for this GPU type
            for status in ["queued", "pending"]:
                try:
                    response = self.reservations_table.query(
                        IndexName="StatusGpuTypeIndex",
                        KeyConditionExpression="#status = :status AND gpu_type = :gpu_type",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={
                            ":status": status,
                            ":gpu_type": gpu_type,
                        },
                    )
                    total_count += len(response.get("Items", []))

                    # Handle pagination for StatusGpuTypeIndex query
                    while "LastEvaluatedKey" in response:
                        response = self.reservations_table.query(
                            IndexName="StatusGpuTypeIndex",
                            KeyConditionExpression="#status = :status AND gpu_type = :gpu_type",
                            ExpressionAttributeNames={"#status": "status"},
                            ExpressionAttributeValues={
                                ":status": status,
                                ":gpu_type": gpu_type,
                            },
                            ExclusiveStartKey=response["LastEvaluatedKey"]
                        )
                        total_count += len(response.get("Items", []))
                except Exception as query_error:
                    # Fallback to scanning if the composite index doesn't exist yet
                    console.print(
                        f"[dim]Fallback: scanning for {status} {gpu_type} reservations[/dim]"
                    )
                    response = self.reservations_table.scan(
                        FilterExpression="contains(#status, :status) AND contains(gpu_type, :gpu_type)",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={
                            ":status": status,
                            ":gpu_type": gpu_type,
                        },
                    )
                    total_count += len(response.get("Items", []))

                    # Handle pagination for fallback scan
                    while "LastEvaluatedKey" in response:
                        response = self.reservations_table.scan(
                            FilterExpression="contains(#status, :status) AND contains(gpu_type, :gpu_type)",
                            ExpressionAttributeNames={"#status": "status"},
                            ExpressionAttributeValues={
                                ":status": status,
                                ":gpu_type": gpu_type,
                            },
                            ExclusiveStartKey=response["LastEvaluatedKey"]
                        )
                        total_count += len(response.get("Items", []))

            return total_count

        except Exception as e:
            console.print(
                f"[red]❌ Error getting queue length for {gpu_type}: {str(e)}[/red]"
            )
            return 0

    def _poll_jupyter_action_result(
        self, reservation_id: str, user_id: str, action: str, timeout_minutes: int = 3
    ) -> bool:
        """Poll reservation table for Jupyter action result"""
        try:
            start_time = time.time()
            timeout_seconds = timeout_minutes * 60

            with Live(console=console, refresh_per_second=2) as live:
                spinner = Spinner(
                    "dots", text=f"🔄 Processing Jupyter {action} request..."
                )
                live.update(spinner)

                initial_state = None

                while time.time() - start_time < timeout_seconds:
                    try:
                        # Get current reservation state - query by user first, then filter by prefix
                        response = self.reservations_table.query(
                            IndexName="UserIndex",
                            KeyConditionExpression="user_id = :user_id",
                            ExpressionAttributeValues={":user_id": user_id},
                        )
                        all_reservations = response.get("Items", [])

                        # Handle pagination for UserIndex query
                        while "LastEvaluatedKey" in response:
                            response = self.reservations_table.query(
                                IndexName="UserIndex",
                                KeyConditionExpression="user_id = :user_id",
                                ExpressionAttributeValues={
                                    ":user_id": user_id},
                                ExclusiveStartKey=response["LastEvaluatedKey"]
                            )
                            all_reservations.extend(response.get("Items", []))

                        # Filter by reservation_id prefix in memory
                        items = [
                            res for res in all_reservations
                            if res.get("reservation_id", "").startswith(reservation_id)
                        ]
                        if len(items) == 0:
                            spinner.text = f"🔄 Waiting for reservation data..."
                            live.update(spinner)
                            time.sleep(2)
                            continue
                        elif len(items) > 1:
                            spinner.text = f"🔄 Multiple reservations found for {reservation_id}, using first match..."
                            live.update(spinner)

                        reservation = items[0]

                        # Capture initial state on first iteration
                        if initial_state is None:
                            initial_state = {
                                "jupyter_enabled": reservation.get(
                                    "jupyter_enabled", False
                                ),
                                "jupyter_url": reservation.get("jupyter_url", ""),
                                "jupyter_port": reservation.get("jupyter_port", 0),
                            }

                        current_jupyter_enabled = reservation.get(
                            "jupyter_enabled", False
                        )
                        jupyter_url = reservation.get("jupyter_url", "")
                        jupyter_port = reservation.get("jupyter_port", 0)

                        # Check if the action has completed
                        if action == "enable":
                            if current_jupyter_enabled and jupyter_url:
                                live.stop()
                                console.print(
                                    f"[green]✅ Jupyter Lab enabled successfully![/green]"
                                )
                                console.print(
                                    f"[cyan]🔗 Jupyter URL:[/cyan] {jupyter_url}"
                                )
                                console.print(
                                    f"[cyan]🔌 Port:[/cyan] {jupyter_port}")
                                return True
                            elif (
                                current_jupyter_enabled
                                != initial_state["jupyter_enabled"]
                            ):
                                spinner.text = f"🔄 Jupyter enabled, waiting for URL..."
                        else:  # disable
                            if not current_jupyter_enabled and not jupyter_url:
                                live.stop()
                                console.print(
                                    f"[green]✅ Jupyter Lab disabled successfully![/green]"
                                )
                                return True
                            elif (
                                current_jupyter_enabled
                                != initial_state["jupyter_enabled"]
                            ):
                                spinner.text = f"🔄 Stopping Jupyter service..."

                        live.update(spinner)
                        time.sleep(3)

                    except Exception as poll_error:
                        console.print(
                            f"[red]❌ Error polling Jupyter status: {poll_error}[/red]"
                        )
                        return False

                # Timeout reached
                live.stop()
                console.print(
                    f"[yellow]⏰ Timeout after {timeout_minutes} minutes[/yellow]"
                )
                console.print(
                    f"[yellow]💡 Use 'gpu-dev show {reservation_id[:8]}' to check Jupyter status[/yellow]"
                )
                return False

        except Exception as e:
            console.print(
                f"[red]❌ Error during Jupyter {action} polling: {str(e)}[/red]"
            )
            return False

    def _poll_add_user_result(
        self, reservation_id: str, user_id: str, github_username: str, timeout_minutes: int = 3
    ) -> bool:
        """Poll reservation table for add user action result"""
        try:
            start_time = time.time()
            timeout_seconds = timeout_minutes * 60

            with Live(console=console, refresh_per_second=2) as live:
                spinner = Spinner(
                    "dots", text=f"🔄 Adding user {github_username}...")
                live.update(spinner)

                initial_secondary_users = None

                while time.time() - start_time < timeout_seconds:
                    try:
                        # Get current reservation state - query by user first, then filter by prefix
                        response = self.reservations_table.query(
                            IndexName="UserIndex",
                            KeyConditionExpression="user_id = :user_id",
                            ExpressionAttributeValues={":user_id": user_id},
                        )
                        all_reservations = response.get("Items", [])

                        # Handle pagination for UserIndex query
                        while "LastEvaluatedKey" in response:
                            response = self.reservations_table.query(
                                IndexName="UserIndex",
                                KeyConditionExpression="user_id = :user_id",
                                ExpressionAttributeValues={
                                    ":user_id": user_id},
                                ExclusiveStartKey=response["LastEvaluatedKey"]
                            )
                            all_reservations.extend(response.get("Items", []))

                        # Filter by reservation_id prefix in memory
                        items = [
                            res for res in all_reservations
                            if res.get("reservation_id", "").startswith(reservation_id)
                        ]
                        if len(items) == 0:
                            spinner.text = f"🔄 Waiting for reservation data..."
                            live.update(spinner)
                            time.sleep(2)
                            continue
                        elif len(items) > 1:
                            spinner.text = f"🔄 Multiple reservations found for {reservation_id}, using first match..."
                            live.update(spinner)

                        reservation = items[0]

                        # Capture initial state on first iteration
                        if initial_secondary_users is None:
                            initial_secondary_users = reservation.get(
                                "secondary_users", []
                            )

                        current_secondary_users = reservation.get(
                            "secondary_users", [])

                        # Check if the user has been added
                        if github_username in current_secondary_users:
                            live.stop()
                            console.print(
                                f"[green]✅ User {github_username} added successfully![/green]"
                            )
                            console.print(
                                f"[cyan]👥 Secondary users:[/cyan] {', '.join(current_secondary_users)}"
                            )
                            return True
                        elif len(current_secondary_users) != len(
                            initial_secondary_users
                        ):
                            spinner.text = (
                                f"🔄 User list updated, verifying {github_username}..."
                            )

                        live.update(spinner)
                        time.sleep(3)

                    except Exception as poll_error:
                        console.print(
                            f"[red]❌ Error polling add user status: {poll_error}[/red]"
                        )
                        return False

                # Timeout reached
                live.stop()
                console.print(
                    f"[yellow]⏰ Timeout after {timeout_minutes} minutes[/yellow]"
                )
                console.print(
                    f"[yellow]💡 Use 'gpu-dev show {reservation_id[:8]}' to check user status[/yellow]"
                )
                return False

        except Exception as e:
            console.print(
                f"[red]❌ Error during add user polling: {str(e)}[/red]")
            return False

    def _poll_extend_action_result(
        self, reservation_id: str, user_id: str, extension_hours: float, timeout_minutes: int = 3, initial_expires_at: str = None
    ) -> bool:
        """Poll reservation table for extend action result"""
        try:
            start_time = time.time()
            timeout_seconds = timeout_minutes * 60

            with Live(console=console, refresh_per_second=2) as live:
                spinner = Spinner(
                    "dots",
                    text=f"🔄 Extending reservation by {extension_hours} hours...",
                )
                live.update(spinner)

                # Use pre-captured initial_expires_at if provided (to avoid race condition)
                initial_expiration = initial_expires_at

                while time.time() - start_time < timeout_seconds:
                    try:
                        # Get current reservation state - query by user first, then filter by prefix
                        response = self.reservations_table.query(
                            IndexName="UserIndex",
                            KeyConditionExpression="user_id = :user_id",
                            ExpressionAttributeValues={":user_id": user_id},
                        )
                        all_reservations = response.get("Items", [])

                        # Handle pagination for UserIndex query
                        while "LastEvaluatedKey" in response:
                            response = self.reservations_table.query(
                                IndexName="UserIndex",
                                KeyConditionExpression="user_id = :user_id",
                                ExpressionAttributeValues={
                                    ":user_id": user_id},
                                ExclusiveStartKey=response["LastEvaluatedKey"]
                            )
                            all_reservations.extend(response.get("Items", []))

                        # Filter by reservation_id prefix in memory
                        items = [
                            res for res in all_reservations
                            if res.get("reservation_id", "").startswith(reservation_id)
                        ]
                        if len(items) == 0:
                            spinner.text = f"🔄 Waiting for reservation data..."
                            live.update(spinner)
                            time.sleep(2)
                            continue
                        elif len(items) > 1:
                            spinner.text = f"🔄 Multiple reservations found for {reservation_id}, using first match..."
                            live.update(spinner)

                        reservation = items[0]

                        # Capture initial expiration on first iteration
                        if initial_expiration is None:
                            initial_expiration = reservation.get(
                                "expires_at", "")

                        current_expiration = reservation.get("expires_at", "")

                        # Check for extension failure indicators
                        last_updated = reservation.get("last_updated", 0)
                        extension_error = reservation.get(
                            "extension_error", "")

                        # If there's an extension error, fail immediately
                        if extension_error:
                            live.stop()
                            console.print(
                                f"[red]❌ Extension failed: {extension_error}[/red]"
                            )
                            return False

                        # Check if the expiration has been updated (different from initial)
                        if (
                            current_expiration != initial_expiration
                            and current_expiration
                        ):
                            live.stop()
                            from datetime import datetime, timezone

                            try:
                                # Treat as naive datetime and manually add UTC timezone (matches list command)
                                naive_dt = datetime.fromisoformat(current_expiration)
                                exp_dt_utc = naive_dt.replace(tzinfo=timezone.utc)
                                # Convert to local timezone
                                local_exp = exp_dt_utc.astimezone()
                                # Format with same style as list command: month-day hour:minute
                                formatted_expiration = local_exp.strftime("%m-%d %H:%M")
                                console.print(
                                    f"[green]✅ Extended reservation {reservation_id} by {extension_hours} hours -- your new expiration is {formatted_expiration}[/green]"
                                )
                                return True
                            except Exception:
                                # Fallback to raw display if parsing fails
                                console.print(
                                    f"[green]✅ Extended reservation {reservation_id} by {extension_hours} hours -- your new expiration is {current_expiration}[/green]"
                                )
                                return True

                        spinner.text = f"🔄 Processing extension request..."
                        live.update(spinner)
                        time.sleep(2)

                    except Exception as poll_error:
                        spinner.text = f"🔄 Checking extension status (retry)..."
                        live.update(spinner)
                        time.sleep(2)

                live.stop()
                console.print(
                    f"[red]❌ Extension request timed out after {timeout_minutes} minutes[/red]"
                )
                console.print(
                    f"[yellow]The extension may still be processing. Check status with: gpu-dev list[/yellow]"
                )
                return False  # Return failure on timeout

        except Exception as e:
            console.print(
                f"[red]❌ Error polling extension result: {str(e)}[/red]")
            return False

    def get_cluster_status(self) -> Optional[Dict[str, Any]]:
        """Get overall GPU cluster status from availability table"""
        try:
            # Get reservations with pagination
            reservations_response = self.reservations_table.scan()
            reservations = reservations_response.get("Items", [])

            # Handle pagination for admin stats scan
            while "LastEvaluatedKey" in reservations_response:
                reservations_response = self.reservations_table.scan(
                    ExclusiveStartKey=reservations_response["LastEvaluatedKey"]
                )
                reservations.extend(reservations_response.get("Items", []))

            # Get total GPUs from availability table
            availability_info = self.get_gpu_availability_by_type()
            total_gpus = 0
            available_gpus = 0

            if availability_info:
                for gpu_type, info in availability_info.items():
                    total_gpus += info.get("total", 0)
                    available_gpus += info.get("available", 0)

            # Calculate stats
            active_reservations = [
                r for r in reservations if r.get("status") == "active"
            ]
            reserved_gpus = sum(int(r.get("gpu_count", 0))
                                for r in active_reservations)

            # Get queue length
            try:
                queue_url = self.config.get_queue_url()
                queue_attrs = self.config.sqs_client.get_queue_attributes(
                    QueueUrl=queue_url, AttributeNames=[
                        "ApproximateNumberOfMessages"]
                )
                queue_length = int(
                    queue_attrs["Attributes"]["ApproximateNumberOfMessages"]
                )
            except:
                queue_length = len(
                    [r for r in reservations if r.get("status") == "pending"]
                )

            return {
                "total_gpus": total_gpus,
                "available_gpus": available_gpus,
                "reserved_gpus": reserved_gpus,
                "active_reservations": len(active_reservations),
                "queue_length": queue_length,
            }

        except Exception as e:
            console.print(
                f"[red]❌ Error getting cluster status: {str(e)}[/red]")
            return None

    def _wait_for_reservations_completion(
        self, reservation_ids: List[str], timeout_minutes: Optional[int] = 10, is_multinode: bool = False, verbose: bool = False
    ) -> Optional[List[Dict[str, Any]]]:
        """Shared polling logic for both single and multinode reservations (always creates SSH config)"""

        status_messages = {
            "pending": "⏳ Reservation request submitted, waiting for processing...",
            "queued": "📋 In queue - waiting for GPU resources...",
            "preparing": "🚀 GPUs found! Preparing your development environment...",
            "creating_server": "🐳 Building custom Docker image...",
            "active": "✅ Reservation complete!",
            "failed": "❌ Reservation failed",
            "cancelled": "🛑 Reservation cancelled",
        }

        start_time = time.time()
        timeout_seconds = timeout_minutes * 60 if timeout_minutes is not None else None
        last_status = None
        last_message = None
        cancelled = False
        close_tool = False
        show_queue_help = True
        queue_state = {"initial_estimated_wait": None,
                       "queue_start_time": None}
        total_nodes = len(reservation_ids)

        # Track previous node statuses to only show changes
        previous_node_statuses = {}

        def handle_interrupt(signum, frame):
            """Handle Ctrl+C to cancel reservation(s)"""
            nonlocal cancelled
            cancelled = True

        def handle_clean_exit(signum, frame):
            """Handle clean exit signal (SIGTERM)"""
            nonlocal close_tool
            close_tool = True
            reservation_text = "reservations" if is_multinode else "reservation"
            console.print(
                f"\n[cyan]🔄 Clean exit requested - keeping {reservation_text} active...[/cyan]"
            )

        def check_keyboard_input():
            """Check if clean exit was requested via signal"""
            return close_tool

        # Set up signal handlers
        signal.signal(signal.SIGTERM, handle_clean_exit)
        try:
            signal.signal(signal.SIGQUIT, handle_clean_exit)
            action_text = "cancel all reservations" if is_multinode else "cancel reservation"
            keep_text = "keep reservations" if is_multinode else "keep reservation"
            console.print(
                f"[dim]💡 Press [cyan]Ctrl+C[/cyan] to {action_text} • Press [cyan]Ctrl+backslash[/cyan] to exit but {keep_text}[/dim]"
            )
        except (AttributeError, OSError):
            action_text = "cancel all reservations" if is_multinode else "cancel reservation"
            keep_text = "keep reservations" if is_multinode else "keep reservation"
            console.print(
                f"[dim]💡 Press [cyan]Ctrl+C[/cyan] to {action_text} • Send [cyan]SIGTERM[/cyan] to exit but {keep_text}[/dim]"
            )
            console.print(
                f"[dim]   (From another terminal: [cyan]kill {os.getpid()}[/cyan])[/dim]"
            )

        # Set up signal handler for Ctrl+C
        old_handler = signal.signal(signal.SIGINT, handle_interrupt)

        try:
            with Live(console=console, refresh_per_second=4) as live:
                initial_text = f"📡 Starting multinode reservation..." if is_multinode else "🔄 Sending reservation request..."
                spinner = Spinner("dots", text=initial_text)
                live.update(spinner)

                while (
                    (timeout_seconds is None or time.time() -
                     start_time < timeout_seconds)
                    and not cancelled
                    and not close_tool
                ):
                    try:
                        # Check for keyboard input (clean exit)
                        if check_keyboard_input():
                            break

                        # Get current status of all reservations
                        all_reservations = []
                        node_details = []

                        for i, res_id in enumerate(reservation_ids):
                            try:
                                response = self.reservations_table.get_item(
                                    Key={"reservation_id": res_id})
                                if "Item" in response:
                                    reservation = response["Item"]
                                    all_reservations.append(reservation)

                                    status = reservation.get(
                                        "status", "unknown")
                                    failure_reason = reservation.get(
                                        "failure_reason", "")
                                    current_detailed_status = reservation.get(
                                        "current_detailed_status", "")
                                    queue_position = reservation.get(
                                        "queue_position", "?")
                                    estimated_wait = reservation.get(
                                        "estimated_wait_minutes", "?")
                                    gpu_count = reservation.get("gpu_count", 1)

                                    # Debug what we're reading from DynamoDB - only show if status changed
                                    if verbose:
                                        node_key = f"node_{i+1}_{res_id[:8]}"
                                        current_node_status = f"status={status}, detailed={current_detailed_status}"
                                        if previous_node_statuses.get(node_key) != current_node_status:
                                            print(
                                                f"[DEBUG] Node {i+1} ({res_id[:8]}): {current_node_status}")
                                            previous_node_statuses[node_key] = current_node_status

                                    node_details.append({
                                        "index": i,
                                        "status": status,
                                        "failure_reason": failure_reason,
                                        "current_detailed_status": current_detailed_status,
                                        "queue_position": queue_position,
                                        "estimated_wait": estimated_wait,
                                        "gpu_count": gpu_count,
                                        "reservation": reservation
                                    })
                                else:
                                    # No reservation found yet, keep waiting
                                    if not is_multinode:
                                        spinner.text = "📡 Waiting for reservation status update..."
                                        live.update(spinner)
                                        time.sleep(2)
                                        continue
                                    else:
                                        node_details.append({
                                            "index": i, "status": "unknown", "failure_reason": "",
                                            "current_detailed_status": "", "queue_position": "?",
                                            "estimated_wait": "?", "gpu_count": 0, "reservation": None
                                        })
                            except Exception as e:
                                if verbose:
                                    print(
                                        f"[DEBUG] Exception querying {res_id[:8]}: {e}")
                                node_details.append({
                                    "index": i, "status": "error", "failure_reason": "Connection error",
                                    "current_detailed_status": "", "queue_position": "?",
                                    "estimated_wait": "?", "gpu_count": 0, "reservation": None
                                })

                        # Calculate aggregate status
                        statuses = [node["status"] for node in node_details]
                        active_count = statuses.count("active")
                        failed_count = statuses.count("failed")
                        cancelled_count = statuses.count("cancelled")
                        preparing_count = statuses.count("preparing")
                        queued_count = statuses.count("queued")

                        # Debug multinode status calculation - only show when aggregate status changes
                        # Only when there are mixed statuses
                        if is_multinode and verbose and len(set(statuses)) > 1:
                            print(
                                f"[DEBUG] Mixed node statuses: active={active_count}, preparing={preparing_count}, queued={queued_count}, failed={failed_count}, total={total_nodes}")

                        # Determine aggregate status for multinode reservations
                        # Only consider it failed if ALL nodes are explicitly failed/cancelled
                        # or if there's a significant portion failed (more than half)
                        if is_multinode:
                            # For multinode, be more conservative about declaring failure
                            if failed_count + cancelled_count >= total_nodes:
                                # All nodes failed - definitely failed
                                aggregate_status = "failed"
                            elif active_count == total_nodes:
                                # All nodes active - success
                                aggregate_status = "active"
                            elif active_count + preparing_count == total_nodes:
                                # All nodes either active or preparing - still working
                                aggregate_status = "preparing" if preparing_count > 0 else "active"
                            elif queued_count > 0:
                                # Any nodes queued - still in queue
                                aggregate_status = "queued"
                            elif failed_count + cancelled_count > total_nodes // 2:
                                # More than half failed - likely a real failure
                                aggregate_status = "failed"
                            else:
                                # Mixed state - keep preparing/pending
                                aggregate_status = "preparing" if preparing_count > 0 else "pending"

                        # Debug aggregate status decision - only show when status changes
                        if is_multinode and verbose and aggregate_status != last_status:
                            print(
                                f"[DEBUG] Calculated aggregate_status: {aggregate_status}")

                        else:
                            # Single node - use original logic
                            if failed_count > 0 or cancelled_count > 0:
                                aggregate_status = "failed"
                            elif active_count == total_nodes:
                                aggregate_status = "active"
                            elif preparing_count > 0:
                                aggregate_status = "preparing"
                            elif queued_count > 0:
                                aggregate_status = "queued"
                            else:
                                # Check for creating_server status
                                creating_server_count = statuses.count(
                                    "creating_server")
                                if creating_server_count > 0:
                                    aggregate_status = "creating_server"
                                else:
                                    aggregate_status = "pending"

                        # Build status message based on aggregate status and mode
                        message = ""

                        if aggregate_status == "queued":
                            # Use first queued node's info for display
                            queued_nodes = [
                                node for node in node_details if node["status"] == "queued"]
                            if queued_nodes:
                                first_queued = queued_nodes[0]
                                queue_position = first_queued["queue_position"]
                                estimated_wait = first_queued["estimated_wait"]

                                # Initialize countdown logic
                                if (
                                    aggregate_status != last_status and estimated_wait != "?"
                                ) or (
                                    estimated_wait != "?"
                                    and queue_state["initial_estimated_wait"] is None
                                ):
                                    try:
                                        wait_minutes = (
                                            int(estimated_wait)
                                            if isinstance(estimated_wait, (int, str))
                                            and str(estimated_wait).isdigit()
                                            else None
                                        )
                                        if wait_minutes is not None:
                                            queue_state["initial_estimated_wait"] = wait_minutes
                                            queue_state["queue_start_time"] = time.time(
                                            )
                                    except (ValueError, TypeError):
                                        pass

                                # Calculate dynamic countdown
                                if (
                                    queue_state["initial_estimated_wait"] is not None
                                    and queue_state["queue_start_time"] is not None
                                ):
                                    elapsed_minutes = (
                                        time.time() -
                                        queue_state["queue_start_time"]
                                    ) / 60
                                    remaining_wait = max(
                                        0,
                                        queue_state["initial_estimated_wait"] -
                                        elapsed_minutes,
                                    )
                                    wait_display = (
                                        f"{remaining_wait:.0f} min"
                                        if remaining_wait > 0
                                        else "Soon"
                                    )
                                else:
                                    wait_display = (
                                        f"{estimated_wait} min"
                                        if estimated_wait != "?"
                                        else "Calculating..."
                                    )

                                if is_multinode:
                                    total_gpus = sum(
                                        node["gpu_count"] for node in node_details if node["reservation"])
                                    message = f"📋 Position #{queue_position} in queue • Estimated wait: {wait_display} • {total_gpus} GPUs across {total_nodes} nodes"
                                else:
                                    gpu_count = first_queued["gpu_count"]
                                    message = f"📋 You are #{queue_position} in queue • Estimated wait: {wait_display} • {gpu_count} GPU(s) requested"

                                # Show help message once when entering queue
                                if show_queue_help and aggregate_status != last_status:
                                    help_text = "\n[dim]💡 Press [cyan]Ctrl+C[/cyan] to cancel reservation • Use [cyan]gpu-dev list[/cyan] to check status[/dim]"
                                    console.print(help_text)
                                    show_queue_help = False
                            else:
                                message = f"📋 Nodes in queue... ({active_count}/{total_nodes} ready)" if is_multinode else "📋 In queue..."

                        elif aggregate_status == "preparing":
                            if is_multinode:
                                # Show detailed preparation info for multinode - show ALL nodes, not just preparing ones
                                detailed_events = []
                                for node in node_details:
                                    node_status = node["status"]
                                    if node_status == "active":
                                        detailed_events.append(f"✓ Ready")
                                    elif node.get("current_detailed_status"):
                                        detailed_events.append(
                                            node["current_detailed_status"])
                                    elif node.get("failure_reason") and node_status == "failed":
                                        detailed_events.append(
                                            node["failure_reason"])
                                    elif node_status == "preparing":
                                        detailed_events.append(
                                            "Preparing environment...")
                                    elif node_status in ["pending", "queued"]:
                                        detailed_events.append(
                                            f"{node_status.title()}...")
                                    else:
                                        detailed_events.append(node_status)

                                # Increased from 4 to 16
                                if detailed_events and len(detailed_events) <= 16:
                                    # For multinode, create a custom multi-line display with individual spinners
                                    from rich.table import Table
                                    from rich.text import Text
                                    from rich.panel import Panel
                                    from rich.console import Group

                                    # Create a list of renderable items
                                    node_lines = []

                                    for i, event in enumerate(detailed_events):
                                        node_num = i + 1
                                        node_status = node_details[i]["status"]

                                        # Create individual spinner or checkmark for each node
                                        if node_status == "active":
                                            # Ready node - show checkmark without spinner
                                            line = Text(
                                                f"✓ Node {node_num}: Ready", style="green")
                                        else:
                                            # Not ready - create a spinner for this specific node
                                            node_spinner = Spinner(
                                                "dots", text=f"Node {node_num}: {event}")
                                            line = node_spinner

                                        node_lines.append(line)

                                    # Group all lines together
                                    group = Group(*node_lines)

                                    # Add summary line
                                    summary = Text(
                                        f"({active_count}/{total_nodes} ready)", style="cyan")
                                    full_display = Group(group, summary)

                                    # Update live display with all spinners
                                    panel = Panel(
                                        full_display, title="🚀 Multinode Setup", expand=False)
                                    live.update(panel)

                                    # Don't set message since we're using custom display
                                    message = None
                                else:
                                    # Summarize if we have many nodes
                                    preparing_count = statuses.count(
                                        "preparing")
                                    message = f"🚀 Preparing {preparing_count} nodes... ({active_count}/{total_nodes} ready)"
                            else:
                                # Show detailed preparation info for single node
                                preparing_nodes = [
                                    node for node in node_details if node["status"] == "preparing"]
                                node = preparing_nodes[0] if preparing_nodes else node_details[0]

                                # Use unified status tracking - prefer current_detailed_status, fall back to failure_reason for actual failures
                                current_detailed_status = node.get(
                                    "current_detailed_status", "")
                                failure_reason = node.get("failure_reason", "") if node.get(
                                    "status") == "failed" else ""

                                if current_detailed_status:
                                    message = f"🚀 {current_detailed_status}"
                                elif failure_reason:
                                    message = f"🚀 Failed: {failure_reason}"
                                else:
                                    message = status_messages.get(
                                        aggregate_status, f"Status: {aggregate_status}")

                        elif aggregate_status == "failed":
                            failed_nodes = [node for node in node_details if node["status"] in [
                                "failed", "cancelled"]]
                            if is_multinode:
                                failure_details = []
                                for node in failed_nodes:
                                    reason = node["failure_reason"] if node["failure_reason"] else node["status"]
                                    failure_details.append(
                                        f"Node {node['index']+1}: {reason}")

                                # Add debug info about all node statuses for troubleshooting
                                debug_details = []
                                for node in node_details:
                                    status = node["status"]
                                    debug_details.append(
                                        f"Node {node['index']+1}: {status}")

                                status_display = "\n".join(
                                    [f"  {detail}" for detail in failure_details])
                                debug_display = "\n".join(
                                    [f"  {detail}" for detail in debug_details])
                                message = f"❌ Multinode failed ({failed_count + cancelled_count}/{total_nodes})\n{status_display}"
                                live.update(Spinner("dots", text=message))
                                time.sleep(2)
                                console.print(
                                    f"\n[red]❌ Multinode reservation failed ({failed_count + cancelled_count}/{total_nodes} nodes failed)[/red]")
                                for detail in failure_details:
                                    console.print(f"[red]  {detail}[/red]")
                                console.print(
                                    f"\n[dim]Debug - All node statuses:[/dim]")
                                for detail in debug_details:
                                    console.print(f"[dim]  {detail}[/dim]")
                                return None
                            else:
                                # Handle single node failure below in completion check
                                pass

                        elif aggregate_status == "active":
                            if is_multinode:
                                # Check if all nodes are truly ready: "active" status AND valid SSH command
                                nodes_ready = 0
                                for node in node_details:
                                    if (node["status"] == "active" and
                                        node["reservation"] and
                                        node["reservation"].get("ssh_command", "ssh user@pending") not in ["ssh user@pending"] and
                                            not node["reservation"].get("ssh_command", "").endswith(".cluster.local")):
                                        nodes_ready += 1

                                if nodes_ready == total_nodes:
                                    # All nodes truly ready with SSH access
                                    live.update(
                                        Spinner("dots", text=f"✅ All {total_nodes} nodes ready!"))
                                    time.sleep(1)
                                    console.print(
                                        f"\n[green]✅ Multinode reservation complete! All {total_nodes} nodes are ready.[/green]")

                                    # Create SSH config files and show connection info for each node
                                    for node in node_details:
                                        if node["reservation"]:
                                            res = node["reservation"]
                                            fqdn = res.get("fqdn")
                                            pod_name = res.get("pod_name")
                                            res_id = res.get("reservation_id")
                                            res_name = res.get("name")

                                            # Create SSH config file for this node
                                            config_path = None
                                            use_include = False
                                            if fqdn and pod_name and res_id:
                                                try:
                                                    config_path, use_include = create_ssh_config_for_reservation(
                                                        fqdn, pod_name, res_id, res_name)
                                                except Exception as e:
                                                    console.print(
                                                        f"[yellow]⚠️  Could not create SSH config for node {node['index']+1}: {str(e)}[/yellow]")

                                            # Show connection info
                                            if config_path and pod_name and use_include:
                                                console.print(
                                                    f"[cyan]🖥️  Node {node['index']+1}:[/cyan] [green]ssh {pod_name}[/green]")
                                            else:
                                                ssh_command = res.get(
                                                    "ssh_command", "ssh user@pending")
                                                ssh_with_forwarding = _add_agent_forwarding_to_ssh(
                                                    ssh_command)
                                                console.print(
                                                    f"[cyan]🖥️  Node {node['index']+1}:[/cyan] {ssh_with_forwarding}")

                                    return all_reservations
                                else:
                                    # Some nodes are "active" but SSH not ready yet - keep preparing
                                    # For multinode, don't override detailed Panel display with summary message
                                    # The preparing logic above will show detailed per-node status
                                    message = f"🚀 Setting up SSH access... ({nodes_ready}/{total_nodes} ready)"
                                    # Don't directly update spinner here - let the main logic handle display
                            else:
                                # Handle single node completion below in completion check
                                pass

                        else:
                            # Default pending/unknown status
                            if is_multinode:
                                message = f"⏳ Processing multinode reservation... ({active_count}/{total_nodes} ready)"
                            else:
                                # Check for detailed status during Docker builds or other detailed operations
                                if aggregate_status == "creating_server" and len(all_reservations) > 0:
                                    reservation = all_reservations[0]
                                    current_detailed_status = reservation.get(
                                        "current_detailed_status", "")
                                    if current_detailed_status:
                                        message = f"🐳 {current_detailed_status}"
                                    else:
                                        message = status_messages.get(
                                            aggregate_status, f"Status: {aggregate_status}")
                                else:
                                    message = status_messages.get(
                                        aggregate_status, f"Status: {aggregate_status}")

                        # Update spinner if status changed, message changed, or we're in certain states
                        # BUT: Don't override custom Panel display for multinode with spinner
                        if (aggregate_status != last_status or
                            message != last_message or
                                aggregate_status in ["queued", "preparing", "creating_server"]):
                            if message and not (is_multinode and aggregate_status == "preparing"):
                                # Only use spinner for single-node or non-preparing multinode states
                                spinner.text = message
                                last_status = aggregate_status
                                last_message = message
                                live.update(spinner)
                            elif not is_multinode and message:
                                # Single node - always use spinner
                                spinner.text = message
                                last_status = aggregate_status
                                last_message = message
                                live.update(spinner)
                            # For multinode preparing with custom display, we already updated above with Panel

                        # Check for single-node completion states (when not multinode or already handled above)
                        if not is_multinode and aggregate_status == "active":
                            reservation = all_reservations[0]
                            ssh_command = reservation.get(
                                "ssh_command", "ssh user@pending")

                            # Only complete if we have a real SSH command (not pending/placeholder)
                            if ssh_command != "ssh user@pending" and not ssh_command.endswith(".cluster.local"):
                                live.stop()
                                duration_hours = reservation.get(
                                    "duration_hours", 8)
                                reservation_id = reservation["reservation_id"]

                                console.print(
                                    f"\n[green]✅ Reservation complete![/green]")
                                console.print(
                                    f"[cyan]📋 Reservation ID:[/cyan] {reservation_id}")
                                console.print(
                                    f"[cyan]⏰ Valid for:[/cyan] {duration_hours} hours")

                                # Show quick connect command
                                short_id = reservation_id[:8]
                                console.print(
                                    f"[cyan]⚡ Quick Connect:[/cyan] [green]gpu-dev connect {short_id}[/green]")

                                # Always create SSH config file for this reservation
                                fqdn = reservation.get("fqdn")
                                pod_name = reservation.get("pod_name")
                                res_id = reservation.get("reservation_id")
                                res_name = reservation.get("name")
                                config_path = None
                                use_include = False
                                if fqdn and pod_name and res_id:
                                    try:
                                        config_path, use_include = create_ssh_config_for_reservation(
                                            fqdn, pod_name, res_id, res_name)
                                    except Exception as e:
                                        console.print(
                                            f"[yellow]⚠️  Could not create SSH config: {str(e)}[/yellow]")

                                # Show SSH command using config file if created, otherwise fallback
                                if config_path and pod_name:
                                    if use_include:
                                        # User approved Include - show simple commands
                                        console.print(
                                            f"[cyan]🖥️  SSH Command:[/cyan] [green]ssh {pod_name}[/green]")
                                        # Create clickable VS Code link
                                        vscode_url = _make_vscode_link(pod_name)
                                        vscode_command = f"code --remote ssh-remote+{pod_name} /home/dev"
                                        console.print(
                                            f"[cyan]💻 VS Code Remote:[/cyan] [link={vscode_url}][green]{vscode_command}[/green][/link]")

                                        # Create clickable Cursor link
                                        cursor_url = _make_cursor_link(pod_name)
                                        cursor_command = f"cursor --remote ssh-remote+{pod_name} /home/dev"
                                        console.print(
                                            f"[cyan]🖥️ Cursor Remote:[/cyan] [link={cursor_url}][green]{cursor_command}[/green][/link]")
                                    else:
                                        # User declined Include - show commands with -F flag
                                        console.print(
                                            f"[cyan]🖥️  SSH Command:[/cyan] [green]ssh -F {config_path} {pod_name}[/green]")
                                        console.print(
                                            f"[cyan]💻 VS Code/Cursor:[/cyan] Add [green]Include ~/.gpu-dev/*-sshconfig[/green] to ~/.ssh/config and ~/.cursor/ssh_config")
                                        console.print(
                                            f"[dim]   Or run: [green]gpu-dev config ssh-include enable[/green][/dim]")
                                else:
                                    # Fallback to full SSH command if config creation failed
                                    ssh_with_forwarding = _add_agent_forwarding_to_ssh(ssh_command)
                                    console.print(
                                        f"[cyan]🖥️  SSH Command:[/cyan] {ssh_with_forwarding}")

                                    vscode_command = _generate_vscode_command(ssh_command)
                                    if vscode_command:
                                        console.print(
                                            f"[cyan]💻 VS Code Remote:[/cyan] {vscode_command}")

                                    cursor_command = _generate_cursor_command(ssh_command)
                                    if cursor_command:
                                        console.print(
                                            f"[cyan]🖱️  Cursor Remote:[/cyan] {cursor_command}")

                                # Show Jupyter link if enabled
                                jupyter_enabled = reservation.get(
                                    "jupyter_enabled", False)
                                jupyter_url = reservation.get(
                                    "jupyter_url", "")
                                if jupyter_enabled and jupyter_url:
                                    console.print(
                                        f"[cyan]📊 Jupyter Lab:[/cyan] {jupyter_url}")

                                return all_reservations
                            else:
                                # Still preparing - show status but don't complete yet
                                current_detailed_status = reservation.get(
                                    "current_detailed_status", "")
                                if current_detailed_status:
                                    message = f"🚀 {current_detailed_status}"
                                else:
                                    message = "🚀 Setting up external SSH access..."

                                if message != (last_status if isinstance(last_status, str) else ""):
                                    spinner.text = message
                                    live.update(spinner)

                        elif not is_multinode and aggregate_status in ["failed", "cancelled"]:
                            live.stop()
                            reservation = all_reservations[0] if all_reservations else {
                            }
                            failure_reason = reservation.get(
                                "failure_reason",
                                reservation.get("current_detailed_status", "Unknown error"))
                            reservation_id = reservation.get(
                                "reservation_id", "unknown")

                            if aggregate_status == "failed":
                                console.print(
                                    f"\n[red]❌ Reservation failed: {failure_reason}[/red]")
                                console.print(
                                    f"[red]📋 Reservation ID: {reservation_id}[/red]")

                                # Show pod logs if available
                                pod_logs = reservation.get("pod_logs", "")
                                if pod_logs and pod_logs.strip():
                                    from rich.panel import Panel
                                    from rich.text import Text

                                    console.print(
                                        "\n[red]🔍 Pod logs (last 20 lines) - Details:[/red]")
                                    log_text = Text(pod_logs)
                                    log_panel = Panel(
                                        log_text,
                                        title="🐚 Container Startup Logs",
                                        title_align="left",
                                        border_style="red",
                                        expand=False,
                                    )
                                    console.print(log_panel)
                            else:
                                console.print(
                                    f"\n[yellow]🛑 Reservation was cancelled[/yellow]")

                            return None

                        # Continue polling
                        time.sleep(3)

                    except Exception as e:
                        console.print(
                            f"\n[red]❌ Error polling reservation status: {str(e)}[/red]")
                        return None

            # Handle cancellation
            if cancelled:
                live.stop()
                action_text = "multinode reservation" if is_multinode else "reservation request"
                console.print(
                    f"\n[yellow]⚠️  Cancelling {action_text}...[/yellow]")

                # Cancel all reservations
                success_count = 0
                for res_id in reservation_ids:
                    try:
                        response = self.reservations_table.get_item(
                            Key={"reservation_id": res_id})
                        if "Item" in response:
                            user_id = response["Item"].get(
                                "user_id", "unknown")
                            if self.cancel_reservation(res_id, user_id):
                                success_count += 1
                    except Exception as e:
                        console.print(
                            f"[red]❌ Error cancelling reservation {res_id[:8]}: {str(e)}[/red]")

                if success_count == len(reservation_ids):
                    success_text = "All reservations cancelled successfully" if is_multinode else "Reservation cancelled successfully"
                    console.print(f"[green]✅ {success_text}[/green]")
                elif success_count > 0:
                    console.print(
                        f"[yellow]⚠️  {success_count}/{len(reservation_ids)} reservations cancelled[/yellow]")
                else:
                    fail_text = "Failed to cancel reservations" if is_multinode else "Failed to cancel reservation"
                    console.print(f"[red]❌ {fail_text}[/red]")

                return None

            # Handle clean exit
            if close_tool:
                live.stop()
                if is_multinode:
                    id_display = ", ".join([res_id[:8]
                                           for res_id in reservation_ids])
                    console.print(
                        f"\n[cyan]📱 Exiting - multinode reservations {id_display} continue in background...[/cyan]")
                else:
                    console.print(
                        f"\n[cyan]📱 Exiting - reservation {reservation_ids[0][:8]} continues in background...[/cyan]")
                console.print(
                    "[cyan]💡 Use 'gpu-dev list' to check status[/cyan]")
                if not is_multinode:
                    console.print(
                        f"[cyan]💡 Use 'gpu-dev show {reservation_ids[0][:8]}' to get connection details when ready[/cyan]")
                return None

            # Timeout reached
            live.stop()
            if timeout_minutes is not None:
                console.print(
                    f"\n[yellow]⏰ Timeout reached after {timeout_minutes} minutes[/yellow]")
            else:
                console.print(
                    f"\n[yellow]⏰ Polling stopped unexpectedly[/yellow]")
            console.print(
                "[yellow]🔍 Check reservation status manually with: gpu-dev list[/yellow]")
            return None

        finally:
            # Restore original signal handlers
            signal.signal(signal.SIGINT, old_handler)
            try:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGQUIT, signal.SIG_DFL)
            except (AttributeError, OSError):
                pass

    def wait_for_reservation_completion(
        self, reservation_id: str, timeout_minutes: Optional[int] = 10, verbose: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Poll for single reservation completion using shared polling logic (always creates SSH config)"""
        results = self._wait_for_reservations_completion(
            [reservation_id], timeout_minutes, is_multinode=False, verbose=verbose)
        return results[0] if results else None
