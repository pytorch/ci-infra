"""Interactive CLI components for GPU Developer CLI"""

import sys
from typing import Dict, List, Optional, Any

try:
    import questionary
    from questionary import Style

    INTERACTIVE_AVAILABLE = True
except ImportError:
    INTERACTIVE_AVAILABLE = False

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Custom style for questionary - softer colors
custom_style = Style(
    [
        ("question", "fg:#5f87af bold"),  # Soft blue
        ("answer", "fg:#5f87af bold"),  # Soft blue
        ("pointer", "fg:#5f87af bold"),  # Soft blue
        ("highlighted", "fg:#5f87af"),  # Soft blue, no bold
        ("selected", "fg:#87af87"),  # Soft green
        ("separator", "fg:#808080"),  # Neutral gray
        ("instruction", ""),
        ("text", ""),
        ("disabled", "fg:#858585 italic"),
    ]
)


def check_interactive_support() -> bool:
    """Check if interactive mode is available"""
    if not INTERACTIVE_AVAILABLE:
        console.print(
            "[red]❌ Interactive mode requires 'questionary'. Install with: pip install questionary[/red]"
        )
        return False

    if not sys.stdin.isatty():
        console.print(
            "[yellow]⚠️  Non-interactive terminal detected. Use command-line flags instead.[/yellow]"
        )
        return False

    return True


def select_gpu_type_interactive(
    availability_info: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Interactive GPU type selection with availability table"""
    if not check_interactive_support():
        return None

    # Hide MIG slice SKUs from the top-level selector — reached via the h100 submenu.
    # Direct `--gpu-type h100-mig-1g` still works for non-interactive scripts.
    visible_info = {
        gt: info for gt, info in availability_info.items()
        if "-mig-" not in gt
    }

    # Aggregate MIG slice availability per parent type, hinted on the h100/b200 rows.
    def _mig_aggregates(parent: str):
        avail = sum(
            int(info.get("available", 0))
            for gt, info in (availability_info or {}).items()
            if gt.startswith(f"{parent}-mig-")
        )
        cap = sum(
            int(info.get("total", 0))
            for gt, info in (availability_info or {}).items()
            if gt.startswith(f"{parent}-mig-")
        )
        return avail, cap

    h100_mig_avail, h100_mig_capacity = _mig_aggregates("h100")
    b200_mig_avail, b200_mig_capacity = _mig_aggregates("b200")
    # Backwards-compat aliases for the existing h100 row code below.
    mig_total_available = h100_mig_avail
    mig_total_capacity = h100_mig_capacity

    # Detect spot types and fetch cross-region spot availability
    from .config import Config, load_config
    _cfg = load_config()
    _env_name = _cfg.user_config.get("environment", "prod")
    _env_config = Config.ENVIRONMENTS.get(_env_name, {})
    _spot_types = set(_env_config.get("spot_types", []))
    is_all_spot = False
    has_spot_types = len(_spot_types) > 0

    # Cross-region: if we're on prod, also fetch prod-east1 spot availability
    spot_region_info = {}
    spot_region_name = None
    if _env_name == "prod":
        east1_env = Config.ENVIRONMENTS.get("prod-east1", {})
        if east1_env:
            spot_region_name = "prod-east1"
            _spot_types = set(east1_env.get("spot_types", []))
            has_spot_types = len(_spot_types) > 0
            try:
                import boto3
                east1_ddb = boto3.resource("dynamodb", region_name=east1_env["region"])
                east1_table = east1_ddb.Table("pytorch-gpu-dev-gpu-availability")
                east1_resp = east1_table.scan()
                for item in east1_resp.get("Items", []):
                    gt = item.get("gpu_type", "")
                    spot_region_info[gt] = {
                        "available": int(item.get("available_gpus", 0)),
                        "total": int(item.get("total_gpus", 0)),
                        "max_reservable": int(item.get("max_reservable", 0)),
                        "queue_length": 0,
                        "estimated_wait_minutes": 0,
                        "running_instances": int(item.get("running_instances", 0)),
                        "desired_capacity": int(item.get("desired_capacity", 0)),
                        "spot_info": item.get("spot_info", {}),
                    }
            except Exception as e:
                pass  # east1 not accessible — show without spot

    # Categorize GPU types into 3 sections
    full_gpus = {}
    mig_gpus = {}
    for gt, info in visible_info.items():
        if "mig" in gt:
            mig_gpus[gt] = info
        else:
            full_gpus[gt] = info

    # Spot types from cross-region (prod-east1) — only non-MIG, non-CPU spot types
    spot_gpus = {k: v for k, v in spot_region_info.items() if k in _spot_types}

    def _format_wait(available, est_wait):
        if available > 0:
            return "Available now", "✅"
        elif est_wait == 0:
            return "Unknown", "⚠️"
        elif est_wait and est_wait < 60:
            return f"{int(est_wait)}min", "⏳"
        elif est_wait and est_wait >= 60:
            h, m = int(est_wait // 60), int(est_wait % 60)
            return f"{h}h{f' {m}min' if m else ''}", "⏳"
        return "Unknown", "⚠️"

    def _format_avail(available, is_maintenance, maintenance_reason):
        if is_maintenance:
            return f"[red]MAINTENANCE[/red]"
        return f"[green]{available}[/green]" if available > 0 else f"[red]{available}[/red]"

    def _build_table(title, items, is_spot=False):
        console.print(f"\n[cyan]{title}[/cyan]")
        table = Table()
        table.add_column("GPU Type", style="cyan")
        table.add_column("Avail", style="green")
        table.add_column("Max\nReservable", style="bright_green")
        table.add_column("Total", style="blue")
        table.add_column("Est. Wait Time", style="magenta")
        for gt, info in items.items():
            avail = info.get("available", 0)
            maint = info.get("maintenance", False)
            maint_reason = info.get("maintenance_reason", "")
            wait_display, _ = _format_wait(avail, info.get("estimated_wait_minutes", 0))
            if maint:
                wait_display = maint_reason or "Under maintenance"
            label = f"{gt.upper()} *" if is_spot else gt.upper()
            table.add_row(
                label,
                _format_avail(avail, maint, maint_reason),
                "-" if maint else str(info.get("max_reservable", 0)),
                str(info.get("total", 0)),
                wait_display,
            )
        console.print(table)

    # Section 1: Full GPUs & CPUs
    _build_table("━━━ Full GPUs & CPUs ━━━", full_gpus)

    # Section 2: MIG Slices
    if mig_gpus:
        console.print("[dim]  Sliced GPUs — isolated fractions of a physical GPU. Perfect for smaller jobs[/dim]")
        console.print("[dim]  that don\'t need full performance or VRAM.[/dim]")
        _build_table("━━━ 🔬 MIG Slices ━━━", mig_gpus)

    # Section 3: Spot Instances (cross-region) — custom table with per-node + price
    if spot_gpus:
        spot_per_node = {"b300": 8, "b200": 8, "h200": 8, "h100": 8, "a100": 8, "t4": 4, "l4": 4}
        console.print(f"\n[cyan]━━━ ⚡ Spot Instances (us-east-1, ~70% cheaper) ━━━[/cyan]")
        st = Table()
        st.add_column("GPU Type", style="cyan")
        st.add_column("Avail\nNow", style="green")
        st.add_column("Per\nNode", style="bright_green")
        st.add_column("Status", style="magenta")
        st.add_column("Availability", style="dim")
        _on_demand = {"b300": 95, "b200": 95, "h200": 55, "h100": 98, "a100": 32, "t4": 4.5, "l4": 7}
        for gt, info in spot_gpus.items():
            avail = info.get("available", 0)
            pn = spot_per_node.get(gt, 8)
            ad = f"[green]{avail}[/green]" if avail > 0 else "[dim]0[/dim]"
            status = "[green]Node up[/green]" if avail > 0 else "Spins up on reserve (~10 min)"
            si = info.get("spot_info", {}) or {}
            # Availability signal from spot price vs on-demand
            sp = si.get("spot_price", "") if isinstance(si, dict) else ""
            if not sp or (isinstance(si, dict) and "No spot data" in str(si.get("spot_signal", ""))):
                avail_signal = "[red]Not offered[/red]"
            else:
                try:
                    ratio = float(sp) / _on_demand.get(gt, 50)
                    pct = int((1 - ratio) * 100)
                    if ratio < 0.4:
                        avail_signal = f"[green]High ({pct}% off)[/green]"
                    elif ratio < 0.7:
                        avail_signal = f"[yellow]Medium ({pct}% off)[/yellow]"
                    else:
                        avail_signal = f"[red]Low ({pct}% off)[/red]"
                except (ValueError, TypeError):
                    avail_signal = "[yellow]Unknown[/yellow]"
            st.add_row(f"{gt.upper()} *", ad, str(pn), status, avail_signal)
        console.print(st)
        console.print("[dim]* = spot: ~70% cheaper, AWS can reclaim with 2-min notice, fulfillment not guaranteed.[/dim]")
        console.print("[dim]  Separate cluster with separate disks. A node spins up when you reserve.[/dim]")

    # Build choices across all 3 sections
    choices = []
    if full_gpus:
        choices.append(questionary.Separator("═══ Full GPUs & CPUs ═══"))
    for gt, info in full_gpus.items():
        avail = info.get("available", 0)
        total = info.get("total", 0)
        maint = info.get("maintenance", False)
        maint_reason = info.get("maintenance_reason", "")
        _, status_indicator = _format_wait(avail, info.get("estimated_wait_minutes", 0))
        ql = info.get("queue_length", 0)
        if maint:
            choices.append(questionary.Choice(
                title=f"🔧 {gt.upper()} - MAINTENANCE: {maint_reason}", value=gt, disabled="Under maintenance"))
        else:
            label = f"{status_indicator} {gt.upper()} ({avail}/{total} available)"
            if ql > 0:
                label += f" - {ql} in queue"
            if gt == "h100" and mig_total_capacity > 0:
                label += f" — also {mig_total_available}/{mig_total_capacity} MIG slices"
            elif gt == "b200" and b200_mig_capacity > 0:
                label += f" — also {b200_mig_avail}/{b200_mig_capacity} MIG slices"
            choices.append(questionary.Choice(title=label, value=gt))

    if mig_gpus:
        choices.append(questionary.Separator("═══ 🔬 MIG Slices (fractional GPUs) ═══"))
        for gt, info in mig_gpus.items():
            avail = info.get("available", 0)
            total = info.get("total", 0)
            _, si = _format_wait(avail, info.get("estimated_wait_minutes", 0))
            choices.append(questionary.Choice(
                title=f"{si} {gt.upper()} ({avail}/{total} available)", value=gt))

    if spot_gpus:
        choices.append(questionary.Separator("═══ ⚡ Spot Instances (us-east-1) ═══"))
        for gt, info in spot_gpus.items():
            avail = info.get("available", 0)
            total = info.get("total", 0)
            _, si = _format_wait(avail, info.get("estimated_wait_minutes", 0))
            choices.append(questionary.Choice(
                title=f"{si} {gt.upper()} * ({avail}/{total} available, spot)", value=f"spot:{gt}"))

    console.print()

    # Interactive selection    console.print()

    # Interactive selection
    try:
        answer = questionary.select(
            "Select GPU type:", choices=choices, style=custom_style
        ).ask()

        return answer
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def _format_eta_seconds(delta_seconds: int) -> str:
    """Format a positive seconds delta as e.g. '12min', '1h24min', '<1min'."""
    if delta_seconds < 60:
        return "<1min"
    if delta_seconds < 3600:
        return f"{delta_seconds // 60}min"
    h = delta_seconds // 3600
    m = (delta_seconds % 3600) // 60
    return f"{h}h" if m == 0 else f"{h}h{m}min"


def select_gpu_count_interactive(
    gpu_type: str,
    max_gpus: int,
    availability_info: Optional[Dict[str, Dict[str, Any]]] = None,
):
    """Interactive GPU count selection.

    Returns int (gpu_count) for normal selections, or a (effective_gpu_type, gpu_count)
    tuple when the user picks a MIG slice option from the h100 submenu.
    """
    if not check_interactive_support():
        return None

    # Generate valid choices based on GPU type limits
    if gpu_type.startswith("cpu-"):
        # CPU instances don't have GPUs, but we still need a "count" for nodes
        valid_counts = [0]  # 0 GPUs for CPU-only instances
        multinode_counts = []  # No multinode for CPU instances
    elif gpu_type in ["t4", "l4", "a10g", "rtxpro6000"]:
        valid_counts = [1, 2, 4]
        # Add multinode options
        multinode_counts = [8, 12, 16, 20, 24]  # multiples of 4
    elif gpu_type == "h100-mig-1g":
        valid_counts = [1, 2, 4, 8]
        multinode_counts = []  # MIG slices live on a single node — no multinode
    elif gpu_type in ["h100-mig-2g", "h100-mig-3g"]:
        valid_counts = [1, 2, 4]
        multinode_counts = []
    elif gpu_type == "g5g":
        valid_counts = [1, 2]
        multinode_counts = [4, 8]  # multiples of 4
    elif gpu_type == "t4-small":
        valid_counts = [1]
        multinode_counts = [2, 3, 4, 5, 6]  # multiples of 1
    else:  # a100, h100, h200, b200
        valid_counts = [1, 2, 4, 8]
        # Add multinode options
        multinode_counts = [16, 24, 32, 40, 48]  # multiples of 8

    # Pull live availability for the parent SKU once — used to annotate every option.
    import time as _time
    parent_info = (availability_info or {}).get(gpu_type, {}) if availability_info else {}
    parent_max_reservable = int(parent_info.get("max_reservable", 0))
    parent_full_nodes = int(parent_info.get("full_nodes_available", 0))
    parent_available = int(parent_info.get("available", 0))
    parent_size_etas = parent_info.get("size_etas", {}) or {}
    _now_ts = int(_time.time())

    # MIG slice submenu: h100 (16+8+8 slices/node) or b200 (4+2+2 slices/node).
    mig_options = []
    mig_spec_map = {
        "h100": [
            ("h100-mig-1g", "10GB", 16),
            ("h100-mig-2g", "20GB", 8),
            ("h100-mig-3g", "40GB", 8),
        ],
        "b200": [
            ("b200-mig-1g", "23GB", 4),
            ("b200-mig-2g", "45GB", 2),
            ("b200-mig-3g", "90GB", 2),
        ],
    }
    for sku, gb, slice_max in mig_spec_map.get(gpu_type, []):
        free = None
        if availability_info and sku in availability_info:
            free = availability_info[sku].get("available", 0)
        for n in [1, 2, 4]:
            if n > slice_max:
                continue
            noun = "slice" if n == 1 else "slices"
            avail_suffix = f"  [{free} free]" if free is not None else ""
            label = f"{n} × {gb} {noun}{avail_suffix}"
            mig_options.append((sku, n, label))

    # Filter single-node by actual max for this GPU type
    valid_counts = [count for count in valid_counts if count <= max_gpus]

    # Add multinode options (multiples of max_gpus)
    multinode_counts = [
        count for count in multinode_counts if count % max_gpus == 0]

    choices = []

    # MIG slice options come first (smallest unit), h100-only.
    if mig_options:
        choices.append(questionary.Separator(
            "--- MIG slices (partial GPU, single node) ---"))
        for sku, count, label in mig_options:
            choices.append(questionary.Choice(title=label, value=(sku, count)))

    # Full single-node options. Header only when slices were rendered above
    # (otherwise the type already implies "Full GPUs").
    if mig_options:
        choices.append(questionary.Separator(
            "--- Full GPUs (single node) ---"))
    for count in valid_counts:
        if count == 1:
            label = f"1 GPU (single node)"
        else:
            label = f"{count} GPUs (single node)"
        if parent_info:
            if parent_max_reservable >= count:
                label += f"  [{parent_available} free]"
            else:
                eta_ts = parent_size_etas.get(str(count))
                try:
                    eta_int = int(eta_ts) if eta_ts is not None else None
                except (TypeError, ValueError):
                    eta_int = None
                if eta_int is not None and eta_int > _now_ts:
                    label += f"  [available in {_format_eta_seconds(eta_int - _now_ts)}]"
                else:
                    label += "  [unavailable now]"
        choices.append(questionary.Choice(title=label, value=count))

    # Multinode at the bottom.
    if multinode_counts:
        choices.append(questionary.Separator(
            "--- Multinode (Distributed) ---"))
        for count in multinode_counts:
            nodes = count // max_gpus
            label = f"{count} GPUs ({nodes} nodes × {max_gpus} GPUs)"
            if parent_info:
                if parent_max_reservable >= count:
                    label += f"  [{parent_full_nodes} full nodes free]"
                else:
                    eta_ts = parent_size_etas.get(str(count))
                    try:
                        eta_int = int(eta_ts) if eta_ts is not None else None
                    except (TypeError, ValueError):
                        eta_int = None
                    if eta_int is not None and eta_int > _now_ts:
                        label += f"  [available in {_format_eta_seconds(eta_int - _now_ts)}]"
                    else:
                        label += "  [unavailable now]"
            choices.append(questionary.Choice(title=label, value=count))

    try:
        if gpu_type.startswith("cpu-"):
            question = f"Reserve {gpu_type.upper()} CPU instance?"
        else:
            question = f"How many {gpu_type.upper()} GPUs?"

        answer = questionary.select(
            question, choices=choices, style=custom_style
        ).ask()

        return answer
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def select_duration_interactive(gpu_type: str = None) -> Optional[float]:
    """Interactive duration selection. CPU types have no duration limit."""
    if not check_interactive_support():
        return None

    is_cpu = gpu_type and gpu_type.startswith("cpu-")

    # Common duration choices
    choices = [
        questionary.Choice("15 minutes", 0.25),
        questionary.Choice("30 minutes", 0.5),
        questionary.Choice("1 hour", 1.0),
        questionary.Choice("2 hours", 2.0),
        questionary.Choice("4 hours", 4.0),
        questionary.Choice("8 hours (default)", 8.0),
        questionary.Choice("12 hours", 12.0),
        questionary.Choice("24 hours" + ("" if is_cpu else " (max)"), 24.0),
    ]
    if is_cpu:
        choices.extend([
            questionary.Choice("48 hours", 48.0),
            questionary.Choice("7 days", 168.0),
            questionary.Choice("30 days", 720.0),
        ])
    choices.append(questionary.Choice("Custom duration", "custom"))

    try:
        answer = questionary.select(
            "How long do you need the reservation?", choices=choices, style=custom_style
        ).ask()

        if answer == "custom":
            max_label = "no limit" if is_cpu else "max 24"
            custom_duration = questionary.text(
                f"Enter duration in hours (decimal allowed, {max_label}):",
                validate=lambda x: _validate_duration(x, unlimited=is_cpu),
                style=custom_style,
            ).ask()

            if custom_duration:
                return float(custom_duration)
            else:
                return None

        return answer
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def select_jupyter_interactive() -> Optional[bool]:
    """Interactive Jupyter Lab selection"""
    if not check_interactive_support():
        return None

    try:
        answer = questionary.confirm(
            "Enable Jupyter Lab? (can be enabled later)",
            default=False,
            style=custom_style,
        ).ask()

        return answer
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def select_reservation_interactive(
    reservations: List[Dict[str, Any]], action: str
) -> Optional[str]:
    """Interactive reservation selection for cancel/edit operations"""
    if not check_interactive_support():
        return None

    if not reservations:
        console.print(
            f"[yellow]No reservations available to {action}.[/yellow]")
        return None

    # Display reservations table
    console.print(
        f"\n[cyan]📋 Your reservations (available to {action}):[/cyan]")

    table = Table()
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("GPUs", style="magenta")
    table.add_column("Status", style="yellow")
    table.add_column("Created", style="blue")
    table.add_column("Expires/ETA", style="red")

    choices = []

    for reservation in reservations:
        try:
            reservation_id = reservation.get("reservation_id", "unknown")
            gpu_count = reservation.get("gpu_count", 1)
            gpu_type = reservation.get("gpu_type", "unknown")
            status = reservation.get("status", "unknown")
            created_at = reservation.get("created_at", "N/A")

            # Format GPU information
            if gpu_type and gpu_type not in ["unknown", "Unknown"]:
                gpu_display = f"{gpu_count}x {gpu_type.upper()}"
            else:
                gpu_display = str(gpu_count)

            # Format expiration time or ETA
            expires_at = reservation.get("expires_at", "N/A")
            if status == "active" and expires_at != "N/A":
                from datetime import datetime

                try:
                    if isinstance(expires_at, str):
                        if expires_at.endswith("Z"):
                            expires_dt_utc = datetime.fromisoformat(
                                expires_at.replace("Z", "+00:00")
                            )
                        elif "+" in expires_at or expires_at.endswith("00:00"):
                            expires_dt_utc = datetime.fromisoformat(expires_at)
                        else:
                            from datetime import timezone

                            naive_dt = datetime.fromisoformat(expires_at)
                            expires_dt_utc = naive_dt.replace(
                                tzinfo=timezone.utc)

                        expires_dt = expires_dt_utc.astimezone()
                        expires_formatted = expires_dt.strftime("%m-%d %H:%M")
                    else:
                        expires_dt = datetime.fromtimestamp(expires_at)
                        expires_formatted = expires_dt.strftime("%m-%d %H:%M")
                except (ValueError, TypeError):
                    expires_formatted = "Invalid"
            elif status in ["queued", "pending"]:
                estimated_wait = reservation.get("estimated_wait_minutes", "?")
                if estimated_wait != "?" and estimated_wait is not None:
                    expires_formatted = f"~{estimated_wait}min"
                else:
                    expires_formatted = "Calculating..."
            else:
                expires_formatted = "N/A"

            # Format created_at datetime
            created_formatted = "N/A"
            if created_at and created_at != "N/A":
                try:
                    from datetime import datetime

                    if isinstance(created_at, str):
                        if created_at.endswith("Z"):
                            created_dt_utc = datetime.fromisoformat(
                                created_at.replace("Z", "+00:00")
                            )
                        elif "+" in created_at or created_at.endswith("00:00"):
                            created_dt_utc = datetime.fromisoformat(created_at)
                        else:
                            from datetime import timezone

                            naive_dt = datetime.fromisoformat(created_at)
                            created_dt_utc = naive_dt.replace(
                                tzinfo=timezone.utc)

                        created_dt = created_dt_utc.astimezone()
                        created_formatted = created_dt.strftime("%m-%d %H:%M")
                    else:
                        created_dt = datetime.fromtimestamp(created_at)
                        created_formatted = created_dt.strftime("%m-%d %H:%M")
                except (ValueError, TypeError):
                    if len(str(created_at)) > 10:
                        created_formatted = str(created_at)[:10]
                    else:
                        created_formatted = str(created_at)

            table.add_row(
                str(reservation_id)[:8],
                gpu_display,
                str(status),
                created_formatted,
                expires_formatted,
            )

            # Create choice for interactive selection
            choice_label = f"{reservation_id[:8]} - {gpu_display} ({status})"
            choices.append(questionary.Choice(
                title=choice_label, value=reservation_id))

        except Exception as row_error:
            console.print(
                f"[yellow]⚠️  Skipping malformed reservation: {str(row_error)}[/yellow]"
            )
            continue

    console.print(table)
    console.print()

    if not choices:
        console.print(
            f"[yellow]No valid reservations found to {action}.[/yellow]")
        return None

    # Add "all" option for cancel action when there are multiple reservations
    if action == "cancel" and len(choices) > 1:
        choices.append(
            questionary.Choice(
                title="🗑️  Cancel ALL reservations above", value="__ALL__"
            )
        )

    # Add quit option at the end for all actions
    action_verb = "cancel" if action == "cancel" else "edit"
    choices.append(
        questionary.Choice(
            title=f"❌  Quit (don't {action_verb} anything)", value="__QUIT__"
        )
    )

    try:
        answer = questionary.select(
            f"Select reservation to {action}:", choices=choices, style=custom_style
        ).ask()

        return answer
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def _validate_duration(duration_str: str, unlimited: bool = False) -> bool:
    """Validate duration input"""
    try:
        duration = float(duration_str)
        if duration < 0.0833:  # Less than 5 minutes
            return "Minimum duration is 5 minutes (0.0833 hours)"
        if not unlimited and duration > 24:
            return "Maximum duration is 24 hours for GPU instances"
        return True
    except ValueError:
        return "Please enter a valid number"


def ask_name_interactive() -> Optional[str]:
    """Ask for optional reservation name"""
    if not check_interactive_support():
        return None

    try:
        answer = questionary.text(
            "Reservation name (optional, press Enter to skip):", style=custom_style
        ).ask()

        # Return None if empty string
        return answer.strip() if answer and answer.strip() else None
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def select_edit_action_interactive() -> Optional[str]:
    """Interactive edit action selection"""
    if not check_interactive_support():
        return None

    choices = [
        questionary.Choice("Enable Jupyter Lab", "enable_jupyter"),
        questionary.Choice("Disable Jupyter Lab", "disable_jupyter"),
        questionary.Choice("Add secondary user", "add_user"),
        questionary.Choice("Extend reservation duration", "extend"),
    ]

    try:
        answer = questionary.select(
            "What would you like to edit?", choices=choices, style=custom_style
        ).ask()

        return answer
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def ask_github_username_interactive() -> Optional[str]:
    """Ask for GitHub username to add"""
    if not check_interactive_support():
        return None

    try:
        answer = questionary.text(
            "Enter GitHub username to add:",
            validate=lambda x: _validate_github_username(x),
            style=custom_style,
        ).ask()

        return answer.strip() if answer else None
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def ask_extension_hours_interactive() -> Optional[float]:
    """Ask for extension hours"""
    if not check_interactive_support():
        return None

    try:
        # Offer common extension choices
        choices = [
            questionary.Choice("1 hour", 1.0),
            questionary.Choice("2 hours", 2.0),
            questionary.Choice("4 hours", 4.0),
            questionary.Choice("8 hours", 8.0),
            questionary.Choice("12 hours", 12.0),
            questionary.Choice("24 hours (max)", 24.0),
            questionary.Choice("Custom extension", "custom"),
        ]

        answer = questionary.select(
            "How many hours to extend?", choices=choices, style=custom_style
        ).ask()

        if answer == "custom":
            # Ask for custom extension
            custom_extension = questionary.text(
                "Enter extension hours (decimal allowed, max 24):",
                validate=lambda x: _validate_extension(x),
                style=custom_style,
            ).ask()

            if custom_extension:
                return float(custom_extension)
            else:
                return None

        return answer
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Selection cancelled.[/yellow]")
        return None


def _validate_github_username(username: str) -> bool:
    """Validate GitHub username format"""
    if not username or not username.strip():
        return "GitHub username cannot be empty"

    username = username.strip()
    if not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
        return "Invalid GitHub username format"

    if len(username) > 39:  # GitHub's max username length
        return "GitHub username too long (max 39 characters)"

    return True


def _validate_extension(hours_str: str) -> bool:
    """Validate extension hours input"""
    try:
        hours = float(hours_str)
        if hours <= 0:
            return "Extension hours must be positive"
        if hours > 24:
            return "Maximum extension is 24 hours"
        return True
    except ValueError:
        return "Please enter a valid number"


def select_disk_interactive(user_id: str, config: Any) -> Optional[str]:
    """
    Interactive disk selection for reserve command.
    Returns:
        - disk_name: User selected an existing disk
        - "__no_disk__": User explicitly chose no disk
        - "__create_new__": User wants to create a new disk (handled internally)
        - "__cancelled__": User cancelled (Ctrl+C or EOF)
    """
    if not check_interactive_support():
        return "__cancelled__"

    from .disks import list_disks

    while True:  # Loop to support "Refresh list"
        try:
            # Get user's disks
            disks = list_disks(user_id, config)

            # Build choices
            choices = []

            if disks:
                # Add header
                choices.append(questionary.Separator("=== Your Disks ==="))

                for disk in disks:
                    disk_name = disk['name']
                    size_gb = disk['size_gb']
                    disk_size = disk.get('disk_size', '')  # Actual used size like "23G"
                    snapshot_count = disk['snapshot_count']

                    # Format display name - show used/total like "23G / 1024GB"
                    if disk_size:
                        size_display = f"{disk_size} / {size_gb}GB"
                    else:
                        size_display = f"{size_gb}GB"
                    display_parts = [f"{disk_name} ({size_display}, {snapshot_count} snapshots)"]

                    # Check if disk is deleted or in use
                    if disk.get('is_deleted', False):
                        display_parts.append("[DELETED]")
                        delete_date = disk.get('delete_date', 'unknown')
                        choices.append(questionary.Choice(
                            title=" ".join(display_parts),
                            value=None,
                            disabled=f"Soft-deleted, expires {delete_date}"
                        ))
                    elif disk['in_use']:
                        display_parts.append("[IN USE]")
                        # Disable this choice
                        choices.append(questionary.Choice(
                            title=" ".join(display_parts),
                            value=None,
                            disabled="Currently in use by another reservation"
                        ))
                    else:
                        choices.append(questionary.Choice(
                            title=" ".join(display_parts),
                            value=disk_name
                        ))

            # Add options for creating new disk or no disk
            choices.append(questionary.Separator("=== Options ==="))
            choices.append(questionary.Choice(
                title="Create new disk",
                value="__create_new__"
            ))
            choices.append(questionary.Choice(
                title="No disk (temporary storage only)",
                value="__no_disk__"
            ))
            choices.append(questionary.Choice(
                title="↻ Refresh list",
                value="__refresh__"
            ))

            # Show selection
            answer = questionary.select(
                "Select a persistent disk:",
                choices=choices,
                style=custom_style,
            ).ask()

            if answer is None:
                # User cancelled (Ctrl+C)
                return "__cancelled__"

            if answer == "__refresh__":
                console.print("[cyan]Refreshing disk list...[/cyan]")
                continue  # Loop back to refresh

            if answer == "__no_disk__":
                # Return special marker to indicate explicit "no disk" choice
                return "__no_disk__"

            if answer == "__create_new__":
                # Ask for disk name
                disk_name = questionary.text(
                    "Enter name for new disk (alphanumeric, hyphens, underscores):",
                    validate=lambda x: _validate_disk_name(x),
                    style=custom_style,
                ).ask()

                if not disk_name:
                    return "__cancelled__"

                # Validate the disk name (actual disk created by Lambda on first use)
                from .disks import create_disk
                success = create_disk(disk_name, user_id, config)
                if success:
                    console.print(f"[cyan]✓ Will create disk '{disk_name}' with this reservation[/cyan]")
                    return disk_name
                else:
                    console.print("[red]Invalid disk name. Continuing without persistent disk.[/red]")
                    return "__cancelled__"

            # Return selected disk name
            return answer

        except EOFError:
            # Handle EOF (e.g., piped input) gracefully
            return "__cancelled__"
        except KeyboardInterrupt:
            # Handle Ctrl+C explicitly
            return "__cancelled__"


def _validate_disk_name(disk_name: str) -> bool:
    """Validate disk name format"""
    if not disk_name or not disk_name.strip():
        return "Disk name cannot be empty"

    disk_name = disk_name.strip()

    # Check alphanumeric + hyphens + underscores
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', disk_name):
        return "Disk name must contain only letters, numbers, hyphens, and underscores"

    if len(disk_name) > 50:
        return "Disk name too long (max 50 characters)"

    return True
