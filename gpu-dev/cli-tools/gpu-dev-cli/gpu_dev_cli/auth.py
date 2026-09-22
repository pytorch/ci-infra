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
_SSH_CACHE_TTL_SECONDS = 14 * 24 * 60 * 60
_SSH_CACHE_PATH = Path(os.path.expanduser("~/.config/gpu-dev/ssh-validation-cache.json"))

# Cache for authenticate_user. STS GetCallerIdentity is stable per AWS profile and slow under SSO
# (~500ms-1.5s). Cache for 24h keyed by AWS_PROFILE; if creds rotate the user_id rarely changes,
# and the next AWS call (DDB/SQS) will surface a credential error if it does.
_AUTH_CACHE_TTL_SECONDS = 60 * 60
_AUTH_CACHE_PATH = Path(os.path.expanduser("~/.config/gpu-dev/auth-cache.json"))


def _auth_cache_key() -> str:
    return os.environ.get("AWS_PROFILE", "default")


def _load_auth_cache(github_user: str) -> Optional[Dict[str, Any]]:
    try:
        if not _AUTH_CACHE_PATH.exists():
            return None
        with open(_AUTH_CACHE_PATH) as f:
            data = json.load(f)
        entry = data.get(_auth_cache_key())
        if not entry or entry.get("github_user") != github_user:
            return None
        if time.time() - float(entry.get("ts", 0)) > _AUTH_CACHE_TTL_SECONDS:
            return None
        # Defense against stale cache on a persistent disk that pre-dates the IRSA fix:
        # if AWS_ROLE_ARN points at a role the cached ARN doesn\'t reference, the cache
        # is from a different identity (e.g. IMDS-fallback before fs_group=1081 landed)
        # and should be ignored.
        expected_role_arn = os.environ.get("AWS_ROLE_ARN", "")
        cached_arn = (entry.get("result") or {}).get("arn", "")
        if expected_role_arn:
            try:
                role_name = expected_role_arn.rsplit("/", 1)[-1]
                if role_name and role_name not in cached_arn:
                    return None
            except Exception:
                pass
        return entry.get("result")
    except Exception:
        return None


def _save_auth_cache(github_user: str, result: Dict[str, Any]) -> None:
    try:
        _AUTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if _AUTH_CACHE_PATH.exists():
            try:
                with open(_AUTH_CACHE_PATH) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[_auth_cache_key()] = {
            "github_user": github_user,
            "ts": int(time.time()),
            "result": result,
        }
        with open(_AUTH_CACHE_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def clear_auth_cache() -> None:
    """Drop the cached auth entry for the current AWS profile. Call this after a credential
    error to force the next authenticate_user() to re-hit STS."""
    try:
        if not _AUTH_CACHE_PATH.exists():
            return
        with open(_AUTH_CACHE_PATH) as f:
            data = json.load(f)
        if _auth_cache_key() in data:
            del data[_auth_cache_key()]
            with open(_AUTH_CACHE_PATH, "w") as f:
                json.dump(data, f)
    except Exception:
        pass


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
    """Authenticate using AWS credentials - if you can call AWS, you're authorized.

    Cached for 24h per AWS profile. The previous SQS get_queue_url probe was dropped:
    it's a redundant permission check; reserve/cancel call SQS directly and surface
    failures themselves, while list/show/avail don't touch SQS at all.
    """
    github_user = config.get_github_username()
    if not github_user:
        raise RuntimeError(
            "GitHub username not configured. Please run: gpu-dev config set github_user <your-github-username>"
        )

    cached = _load_auth_cache(github_user)
    if cached is not None:
        return cached

    try:
        identity = config.get_user_identity()
        arn = identity["arn"]
        user_name = arn.split("/")[-1]
        result = {
            "user_id": user_name,
            "github_user": github_user,
            "arn": arn,
        }
        _save_auth_cache(github_user, result)
        return result
    except Exception as e:
        clear_auth_cache()
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
