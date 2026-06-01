"""Minimal configuration for GPU Dev CLI - Zero setup required"""

import os
import json
import boto3
import botocore.exceptions
from pathlib import Path
from typing import Dict, Any, Optional


class Config:
    """Zero-config AWS-based configuration"""

    # Environment configurations (test vs prod)
    ENVIRONMENTS = {
        "test": {
            "region": "us-west-1",
            "workspace": "default",
            "description": "Test environment",
        },
        "prod": {
            "region": "us-east-2",
            "workspace": "prod",
            "description": "Production environment",
        },
        "prod-east1": {
            "region": "us-east-1",
            "workspace": "prod-east1",
            "description": "Spot-only us-east-1 environment (T4/L4/CPU)",
            "spot_types": ["b300", "b200", "h200", "h100", "a100", "t4", "l4", "rtxpro6000"],
        },
        # Staging (us-west-1, tf workspace "test"). Same standard resource prefix
        # as prod, just a different region — so only the region changes. Live
        # capacity: cpu-x86/arm + t4. Used for integration tests. Select via
        # `GPU_DEV_ENVIRONMENT=staging` (or the legacy "test" env, same target).
        "staging": {
            "region": "us-west-1",
            "workspace": "test",
            "description": "Staging (us-west-1, cpu + t4)",
        },
    }
    DEFAULT_ENVIRONMENT = "prod"

    # Config file path (class-level for access without instantiation)
    CONFIG_FILE = Path.home() / ".config" / "gpu-dev" / "config.json"

    # Legacy paths for migration
    LEGACY_CONFIG_FILE = Path.home() / ".gpu-dev-config"
    LEGACY_ENVIRONMENT_FILE = Path.home() / ".gpu-dev-environment.json"

    def __init__(self):
        # Load unified config (handles migration from legacy files)
        self.user_config = self._load_config()

        # Active environment: GPU_DEV_ENVIRONMENT env wins (handy for tests/CI),
        # then the persisted config, then the default. Its region/prefix back the
        # fallbacks below so e.g. `GPU_DEV_ENVIRONMENT=staging` reaches us-west-2.
        env_override = os.getenv("GPU_DEV_ENVIRONMENT")
        env_name = env_override or self.user_config.get(
            "environment", self.DEFAULT_ENVIRONMENT)
        env_cfg = self.ENVIRONMENTS.get(env_name, {})

        # Get region: AWS_* env vars take priority (for spot routing); then an
        # explicit GPU_DEV_ENVIRONMENT switch uses that env's region (beating the
        # persisted one); then the persisted config; then the env's region; default.
        env_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        if env_region and env_region != self.user_config.get("region"):
            self.aws_region = env_region
        elif env_override and env_cfg.get("region"):
            self.aws_region = env_cfg["region"]
        elif self.user_config.get("region"):
            self.aws_region = self.user_config["region"]
        elif env_cfg.get("region"):
            self.aws_region = env_cfg["region"]
        else:
            self.aws_region = "us-east-2"

        os.environ["AWS_DEFAULT_REGION"] = self.aws_region

        # Resource naming convention — per-environment prefix (default for prod).
        self.prefix = env_cfg.get("prefix", "pytorch-gpu-dev")

        # Construct ARNs from convention
        self.queue_name = f"{self.prefix}-reservation-queue"
        self.reservations_table = f"{self.prefix}-reservations"
        self.disks_table = f"{self.prefix}-disks"
        self.operations_table = f"{self.prefix}-operations"
        self.availability_table = f"{self.prefix}-gpu-availability"
        self.cluster_name = f"{self.prefix}-cluster"

        # Determine AWS session (with profile support)
        self.session = self._create_aws_session()

        # AWS clients
        self._sts_client = None
        self._sqs_client = None
        self._dynamodb = None

    _CRED_CACHE = Path.home() / ".config" / "gpu-dev" / "aws-cred-cache.json"

    def _create_aws_session(self):
        """Create AWS session, caching resolved credentials to skip SSO resolution (~900ms)."""
        import time as _time

        # Try cached credentials first (avoids 900ms SSO resolution)
        try:
            if self._CRED_CACHE.exists():
                cached = json.loads(self._CRED_CACHE.read_text())
                if _time.time() < cached.get("expires", 0):
                    return boto3.Session(
                        aws_access_key_id=cached["access_key"],
                        aws_secret_access_key=cached["secret_key"],
                        aws_session_token=cached["token"],
                        region_name=self.aws_region,
                    )
        except Exception:
            pass

        # Resolve credentials from SSO/profile (slow path, ~900ms)
        try:
            session = boto3.Session(profile_name="gpu-dev")
            creds = session.get_credentials()
            if not creds:
                raise Exception("no credentials")
        except Exception:
            session = boto3.Session()
            creds = session.get_credentials()

        # Cache resolved credentials (safe — they're short-lived STS tokens)
        try:
            frozen = creds.get_frozen_credentials()
            if frozen.token:
                self._CRED_CACHE.parent.mkdir(parents=True, exist_ok=True)
                self._CRED_CACHE.write_text(json.dumps({
                    "access_key": frozen.access_key,
                    "secret_key": frozen.secret_key,
                    "token": frozen.token,
                    "expires": _time.time() + 2700,  # cache 45min (SSO tokens last ~1h)
                }))
                self._CRED_CACHE.chmod(0o600)
        except Exception:
            pass

        return session

    def refresh_session(self):
        """Clear cached credentials and re-resolve. Called on ExpiredTokenException."""
        try:
            self._CRED_CACHE.unlink(missing_ok=True)
        except Exception:
            pass
        self.session = self._create_aws_session()
        self._sts_client = None
        self._sqs_client = None
        self._dynamodb = None

    @property
    def sts_client(self):
        if self._sts_client is None:
            self._sts_client = self.session.client("sts", region_name=self.aws_region)
        return self._sts_client

    @property
    def sqs_client(self):
        if self._sqs_client is None:
            self._sqs_client = self.session.client("sqs", region_name=self.aws_region)
        return self._sqs_client

    @property
    def dynamodb(self):
        if self._dynamodb is None:
            self._dynamodb = self.session.resource(
                "dynamodb", region_name=self.aws_region
            )
        return self._dynamodb

    def get_queue_url(self) -> str:
        """Get SQS queue URL by name"""
        try:
            response = self.sqs_client.get_queue_url(QueueName=self.queue_name)
            return response["QueueUrl"]
        except Exception as e:
            raise RuntimeError(
                f"Cannot access SQS queue {self.queue_name}. Check AWS permissions: {e}"
            )

    def get_user_identity(self) -> Dict[str, Any]:
        """Get current AWS user identity"""
        try:
            response = self.sts_client.get_caller_identity()
            return {
                "user_id": response["UserId"],
                "account": response["Account"],
                "arn": response["Arn"],
            }
        except Exception as e:
            raise RuntimeError(
                f"Cannot get AWS caller identity. Check AWS credentials: {e}"
            )

    def _load_config(self) -> Dict[str, Any]:
        """Load unified config from ~/.config/gpu-dev/config.json

        Migrates from legacy locations if needed:
        - ~/.gpu-dev-config (user config)
        - ~/.gpu-dev-environment.json (environment config)

        Ensures required environment keys exist, filling from defaults.
        """
        config = {}
        needs_save = False

        # Try to load existing config
        if self.CONFIG_FILE.exists():
            try:
                with open(self.CONFIG_FILE, "r") as f:
                    config = json.load(f)
            except Exception as e:
                print(f"Warning: Could not load config: {e}")
        else:
            # Migrate from legacy files
            migrated_from = []

            if self.LEGACY_CONFIG_FILE.exists():
                try:
                    with open(self.LEGACY_CONFIG_FILE, "r") as f:
                        config.update(json.load(f))
                    migrated_from.append(str(self.LEGACY_CONFIG_FILE))
                except Exception as e:
                    print(f"Warning: Could not read {self.LEGACY_CONFIG_FILE}: {e}")

            if self.LEGACY_ENVIRONMENT_FILE.exists():
                try:
                    with open(self.LEGACY_ENVIRONMENT_FILE, "r") as f:
                        config.update(json.load(f))
                    migrated_from.append(str(self.LEGACY_ENVIRONMENT_FILE))
                except Exception as e:
                    print(f"Warning: Could not read {self.LEGACY_ENVIRONMENT_FILE}: {e}")

            if migrated_from:
                print(
                    f"Migrated config from {', '.join(migrated_from)} "
                    f"to {self.CONFIG_FILE}"
                )
                needs_save = True

        # Ensure required environment keys exist
        default_env = self.ENVIRONMENTS[self.DEFAULT_ENVIRONMENT]
        if "region" not in config:
            config["region"] = default_env["region"]
            needs_save = True
        if "environment" not in config:
            config["environment"] = self.DEFAULT_ENVIRONMENT
            needs_save = True
        if "workspace" not in config:
            config["workspace"] = default_env["workspace"]
            needs_save = True

        # Save if we added any defaults or migrated
        if needs_save:
            try:
                self._save_config(config)
            except Exception as e:
                print(f"Warning: Could not save config: {e}")

        return config

    def _save_config(self, config: Dict[str, Any]) -> None:
        """Save config dict to file."""
        self.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(self.CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)

    def save_config(self, key: str, value: Any) -> None:
        """Save a configuration value."""
        self.user_config[key] = value
        self._save_config(self.user_config)

    def set_environment(self, env_name: str) -> Dict[str, Any]:
        """Set the environment (test or prod).

        Args:
            env_name: Environment name ('test' or 'prod')

        Returns:
            The environment config dict

        Raises:
            ValueError: If env_name is not valid

        Note:
            This does not invalidate cached AWS clients. In the CLI, this is
            fine since each command is a separate process. If using Config
            as a library, create a new instance after changing environments.
        """
        if env_name not in self.ENVIRONMENTS:
            raise ValueError(
                f"Invalid environment: {env_name}. "
                f"Must be one of: {list(self.ENVIRONMENTS.keys())}"
            )

        env_config = self.ENVIRONMENTS[env_name]

        # Update config with environment settings
        self.user_config["environment"] = env_name
        self.user_config["region"] = env_config["region"]
        self.user_config["workspace"] = env_config["workspace"]

        self._save_config(self.user_config)
        self.aws_region = env_config["region"]
        os.environ["AWS_DEFAULT_REGION"] = self.user_config["region"]

        return env_config

    def get(self, key: str) -> Optional[Any]:
        """Get a config value."""
        return self.user_config.get(key)

    def get_github_username(self) -> Optional[str]:
        """Get GitHub username, falling back to GPU_DEV_GITHUB_USER env var.

        Lambda sets GPU_DEV_GITHUB_USER on every pod from the reservation's
        github_user field, so a user running gpu-dev from inside their dev pod
        doesn\'t have to `gpu-dev config set github_user <name>` first.
        """
        v = self.user_config.get("github_user")
        if v:
            return v
        v = os.environ.get("GPU_DEV_GITHUB_USER")
        return v or None


def load_config() -> Config:
    """Load zero-config setup"""
    return Config()
