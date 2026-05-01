"""Minimal AWS-only authentication for GPU Dev CLI"""

import json
import os
import subprocess
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional
from .config import Config
from rich.spinner import Spinner

# SSH validation result is cached locally for 24h. New keys pushed to GitHub still take effect
# at reservation time (pods fetch live keys via init container) — caching only skips the
# pre-flight "are you who you say you are" check.
_SSH_CACHE_TTL_SECONDS = 24 * 60 * 60
_SSH_CACHE_PATH = Path(os.path.expanduser("~/.config/gpu-dev/ssh-validation-cache.json"))


def _load_ssh_cache(github_user: str) -> Optional[Dict[str, Any]]:
    """Return cached validation if it's fresh and matches the configured github_user, else None."""
    try:
        if not _SSH_CACHE_PATH.exists():
            return None
        with open(_SSH_CACHE_PATH) as f:
            data = json.load(f)
        if data.get("configured_user") != github_user:
            return None
        if time.time() - float(data.get("ts", 0)) > _SSH_CACHE_TTL_SECONDS:
            return None
        return data.get("result")
    except Exception:
        return None


def _save_ssh_cache(github_user: str, result: Dict[str, Any]) -> None:
    """Persist a successful validation result. Failures are not cached (so they can recover)."""
    if not result.get("valid"):
        return
    try:
        _SSH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_SSH_CACHE_PATH, "w") as f:
            json.dump({
                "configured_user": github_user,
                "ts": int(time.time()),
                "result": result,
            }, f)
    except Exception:
        pass


def authenticate_user(config: Config) -> Dict[str, Any]:
    """Authenticate using AWS credentials - if you can call AWS, you're authorized"""
    try:
        # Test AWS access by getting caller identity
        identity = config.get_user_identity()

        # Test specific resource access by trying to get queue URL
        config.get_queue_url()

        # Extract user info from AWS ARN
        arn = identity["arn"]
        user_name = arn.split("/")[-1]  # Extract username from ARN

        # Get GitHub username from config
        github_user = config.get_github_username()
        if not github_user:
            raise RuntimeError(
                f"GitHub username not configured. Please run: gpu-dev config set github_user <your-github-username>"
            )

        return {
            "user_id": user_name,  # AWS username for reservation ownership
            "github_user": github_user,  # GitHub username for SSH keys
            "arn": arn,
        }

    except Exception as e:
        raise RuntimeError(f"AWS authentication failed: {e}")


def validate_ssh_key_matches_github_user(config: Config, live=None) -> Dict[str, Any]:
    """
    Validate that the SSH key matches the configured GitHub username

    Returns:
        Dict with validation results:
        - "valid": bool - Whether SSH key matches configured username
        - "configured_user": str - Username from config
        - "ssh_user": str or None - Username detected from SSH
        - "error": str or None - Error message if validation failed
    """
    try:
        # Get configured GitHub username
        github_user = config.get_github_username()
        if not github_user:
            return {
                "valid": False,
                "configured_user": None,
                "ssh_user": None,
                "error": "GitHub username not configured. Run: gpu-dev config set github_user <username>",
            }

        # Cache short-circuit — skip the SSH handshake (~1-3s) if we recently validated this user.
        # Cache TTL is 24h. New keys pushed to GitHub still take effect at reservation time
        # (pods fetch live keys via init container), so caching the pre-flight check is safe.
        cached = _load_ssh_cache(github_user)
        if cached is not None:
            return cached

        # Run ssh git@github.com with interactive host verification support
        ssh_output = None

        try:
            # Use interactive SSH to allow password-protected keys
            # Stop the spinner to allow password prompt if needed
            if live:
                live.stop()

            # Run SSH without BatchMode to allow password prompts
            # Use stderr redirection to a pipe but keep stdin/stdout for interactive prompts
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w+', delete=False) as stderr_file:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", "git@github.com"],
                    stdin=None,  # Use terminal stdin for password prompt
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    text=True,
                    timeout=30,
                )

                # Read stderr output
                stderr_file.seek(0)
                ssh_output = stderr_file.read()

            # Clean up temp file
            import os
            try:
                os.unlink(stderr_file.name)
            except:
                pass

            # Restart the spinner
            if live:
                live.start()

            # Check if we got the expected GitHub response
            if "Hi " in ssh_output and "You've successfully authenticated" in ssh_output:
                # Success case - continue to parse username
                pass
            elif "Host key verification failed" in ssh_output:
                raise subprocess.CalledProcessError(result.returncode, "ssh", "Host verification failed")

        except subprocess.TimeoutExpired:
            return {
                "valid": False,
                "configured_user": github_user,
                "ssh_user": None,
                "error": "SSH connection timed out - please check your connection",
            }
        except Exception as e:
            return {
                "valid": False,
                "configured_user": github_user,
                "ssh_user": None,
                "error": f"SSH connection failed: {str(e)}",
            }

        # Ensure ssh_output is not None
        if ssh_output is None:
            ssh_output = ""

        # Parse GitHub SSH response to extract username
        # Expected format: "Hi <username>! You've successfully authenticated, but GitHub does not provide shell access."
        username_match = re.search(r"Hi ([^!]+)!", ssh_output)

        if not username_match:
            return {
                "valid": False,
                "configured_user": github_user,
                "ssh_user": None,
                "error": f"Could not parse GitHub SSH response. Output: {ssh_output[:200]}",
            }

        ssh_detected_user = username_match.group(1).strip()

        # Compare usernames (case-insensitive)
        is_valid = ssh_detected_user.lower() == github_user.lower()

        result = {
            "valid": is_valid,
            "configured_user": github_user,
            "ssh_user": ssh_detected_user,
            "error": None
            if is_valid
            else f"SSH key belongs to '{ssh_detected_user}' but configured user is '{github_user}'",
        }
        _save_ssh_cache(github_user, result)
        return result

    except Exception as e:
        return {
            "valid": False,
            "configured_user": github_user if "github_user" in locals() else None,
            "ssh_user": None,
            "error": f"Validation error: {str(e)}",
        }
