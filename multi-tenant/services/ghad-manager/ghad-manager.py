from datetime import datetime, timedelta, timezone
import grp
import logging
import os
import pwd
from typing import List
import subprocess
import sys
import time
import socket
import json

import docker
from github import Auth, Github


logger = logging.getLogger(__name__)


# The name of the container that should always be running
REQUIRED_CONTAINER_NAME = "ghad-main-shared-instance-container"
DOCKER_REPOSITORY = "308535385114.dkr.ecr.us-east-1.amazonaws.com"
DOCKER_TAG = "latest"
MAX_RUNNER_CONTAINER_AGE = timedelta(hours=10)


class UserSocketsPath:
    def __init__(self, uid: int, socket_path: str, user_name: str):
        self._uid = uid
        self._socket_path = socket_path
        self._user_name = user_name
        self._docker_client = None

    @property
    def uid(self) -> int:
        return self._uid

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def docker_client(self) -> docker.DockerClient:
        if not self._docker_client:
            self._docker_client = get_docker_client(self.socket_path)
        return self._docker_client


def get_home_users() -> list[pwd.struct_passwd]:
    users = []
    for user in pwd.getpwall():
        if os.path.isdir(f"/home/{user.pw_name}") and user.pw_name not in [
            "ubuntu",  # default user
            "ssm-user",  # AWS default user for session management
            "pytorch",  # default user on DGX
            "nvadmin",
        ]:
            users.append(user)
    return users


def check_socket_for_users(timeout=10 * 60, check_interval=1) -> List[UserSocketsPath]:
    users = get_home_users()
    user_sockets = {
        user.pw_uid: (
            False,
            user.pw_name,
        )
        for user in users
    }
    start_time = time.time()

    logger.info(
        f"Checking for Docker sockets for users: {', '.join([user.pw_name for user in users])}"
    )

    while time.time() - start_time < timeout:
        for user in users:
            if user_sockets.get(
                user.pw_uid,
                (
                    False,
                    "",
                ),
            )[0]:
                continue

            uid = user.pw_uid
            socket_path = f"/run/user/{uid}/docker.sock"

            if os.path.exists(socket_path):
                user_sockets[uid] = (
                    True,
                    user.pw_name,
                )

        if all(has_socket for has_socket, _ in user_sockets.values()):
            logger.info("All Docker sockets found. Starting manager...")
            break
        else:
            logger.debug(
                f"Still missing Docker sockets for users: {[uid for uid, has_socket in user_sockets.items() if not has_socket]}"
            )

        time.sleep(check_interval)

    return [
        UserSocketsPath(uid, f"/run/user/{uid}/docker.sock", user_name)
        for (
            uid,
            (
                has_socket,
                user_name,
            ),
        ) in user_sockets.items()
        if has_socket
    ]


def get_docker_client(socket_path: str) -> docker.DockerClient | None:
    try:
        logger.info(f"Connecting to Docker socket {socket_path}")
        client = docker.DockerClient(base_url=f"unix://{socket_path}")
        return client
    except Exception as e:
        logger.info(f"Error connecting to Docker socket {socket_path}: {str(e)}")
        return None


def is_container_running(client: docker.DockerClient, container_name: str) -> bool:
    try:
        containers = client.containers.list(filters={"name": container_name}, all=True)
        for container in containers:
            if container.name == container_name and container.status == "running":
                return True
        return False
    except Exception as e:
        logger.warning(f"Error checking containers: {str(e)}")
        return False


def recycle_idle_runner(
    client: docker.DockerClient,
    container_name: str,
    now: datetime | None = None,
) -> None:
    """Restart an idle runner before its copied ECR credential expires."""
    now = now or datetime.now(timezone.utc)
    try:
        containers = client.containers.list(filters={"name": container_name}, all=True)
        for container in containers:
            if container.name != container_name or container.status != "running":
                continue

            created_at = datetime.fromisoformat(
                container.attrs["Created"].replace("Z", "+00:00")
            )
            if now - created_at < MAX_RUNNER_CONTAINER_AGE:
                return

            processes = container.top().get("Processes", [])
            if any("Runner.Worker" in field for row in processes for field in row):
                return

            logger.info(f"Recycling idle runner container {container_name}")
            container.stop()
            return
    except Exception as e:
        logger.warning(f"Error recycling idle runner {container_name}: {str(e)}")


def stop_all_containers(client: docker.DockerClient, uid: int) -> None:
    logger.info(f"Stopping all containers for user {uid}")
    try:
        containers = client.containers.list(all=True)
        for container in containers:
            try:
                container.stop()
                container.remove()
            except Exception as e:
                logger.warning(f"Error stopping container {container.name}: {str(e)}")
    except Exception as e:
        logger.warning(f"Error stopping all containers: {str(e)}")


def prune_images(client: docker.DockerClient, uid: int) -> None:
    logger.info(f"Pruning images for {uid}, so that we don't run out of disk space.")
    client.images.prune({"dangling": False})


def prune_networks(client: docker.DockerClient, uid: int) -> None:
    # GitHub Actions creates a `github_network_*` bridge per job (for jobs that
    # use `container:`/service containers) against this user's rootless dockerd.
    # When the runner's "Stop containers" post-step doesn't tear it down, the
    # network leaks; once ~30 accumulate, Docker's default address pool is
    # exhausted and every subsequent "Initialize containers" fails with
    # "all predefined address pools have been fully subnetted". Reclaim the
    # orphaned networks here (predefined bridge/host/none are never removed).
    logger.info(f"Pruning unused networks for {uid} to free Docker address pools.")
    try:
        client.networks.prune()
    except Exception as e:
        logger.warning(f"Error pruning networks for {uid}: {str(e)}")


def start_container_if_not_running(
    runner_url: str,
    instance_label: str,
    uid: int,
    client: docker.DockerClient,
    docker_group_id: int,
    container_name: str,
    user_name: str,
    docker_tag: str,
) -> None:
    recycle_idle_runner(client, container_name)
    if not is_container_running(client, container_name):
        stop_all_containers(client, uid)
        prune_images(client, uid)
        # Runs after stop_all_containers so leaked github_network_* bridges are
        # unreferenced and can be reclaimed before the pool exhausts.
        prune_networks(client, uid)
        # Clearing the local Docker credential to refresh a new one
        subprocess.run(
            [
                "rm",
                "-rf",
                os.path.join("/home", user_name, ".docker", "config.json"),
            ]
        )
        for registry in [DOCKER_REPOSITORY, "public.ecr.aws/q9t5s3a7"]:
            login_to_ecr(client, registry, user_name)

        login_to_dockerhub(client, user_name, uid)
        logger.info(
            f"Container {container_name} for user {uid} is not running, attempting to start it."
        )
        logger.info("Getting GH token")
        gh_token = get_gh_runner_token()
        logger.info("GH token obtained, starting container.")
        _work_dir = os.path.join("/home", user_name, "_work")
        # NOTE: This contains things like node that is used for running workflows with `container`
        externals_dir = os.path.join("/home", user_name, "externals")
        try:
            for _dir in [_work_dir, externals_dir]:
                subprocess.run(["rm", "-rf", _dir])
                subprocess.run(["mkdir", "-p", _dir])
                subprocess.run(["chown", "-R", f"{uid}:{uid}", _dir])
        except Exception as e:
            logger.warning(
                f"Error setting up container environment on host user {container_name}: {str(e)}"
            )

        _cache_dir = "/mnt/hf_cache/"
        # Set cache dir permission before each job, hacky but it works for now
        subprocess.run(
            [
                "find",
                _cache_dir,
                "-type",
                "d",
                "-exec",
                "chmod",
                "0777",
                "{}",
                ";",
            ]
        )
        subprocess.run(
            [
                "find",
                _cache_dir,
                "-type",
                "f",
                "-exec",
                "chmod",
                "0777",
                "{}",
                ";",
            ]
        )

        try:
            response = client.containers.run(
                image=f"{DOCKER_REPOSITORY}/multi-tenant-gpu:{docker_tag}",
                name=container_name,
                command=f'/bin/bash /multi-tenant-gpu-main.sh "{user_name}" "{docker_group_id}" "{runner_url}" "{gh_token}" "{socket.gethostname()}" "{uid}" "{instance_label}"',
                volumes={
                    f"/run/user/{uid}/docker.sock": {
                        "bind": "/var/run/docker.sock",
                        "mode": "rw",
                    },
                    _work_dir: {"bind": _work_dir, "mode": "rw"},
                    # Mount to the same path as the FSx volume in regular CI jobs
                    _cache_dir: {"bind": _cache_dir, "mode": "rw"},
                    externals_dir: {"bind": externals_dir, "mode": "rw"},
                    f"/home/{user_name}/multi-tenant-gpu-main.sh": {
                        "bind": "/multi-tenant-gpu-main.sh",
                        "mode": "ro",
                    },
                    f"/home/{user_name}/.docker/config.json": {
                        "bind": "/docker/config.json",
                        "mode": "ro",
                    },
                },
                device_requests=[
                    docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                ],
                remove=True,
                detach=True,
            )
            logger.info(f"the response from the docker daemon: {response}")
        except Exception as e:
            logger.warning(f"Error starting container {container_name}: {str(e)}")


def login_to_ecr(client: docker.DockerClient, registry: str, user_name: str) -> None:
    logger.info(f"Logging into {registry}")
    token = subprocess.check_output(
        [
            "aws",
            "ecr-public" if "public.ecr.aws" in registry else "ecr",
            "get-login-password",
            "--region",
            "us-east-1",
        ],
        text=True,
    ).strip()

    response = client.login(username="AWS", password=token, registry=registry)
    if response.get("Status") == "Login Succeeded":
        logger.info(f"Login to {registry} succeeded.")
    else:
        logger.error(f"Login to {registry} failed: {response}")
        # Calling an exit here effectively restarts the daemon
        sys.exit(1)

    # Write the credential to ~/.docker/config.json
    subprocess.check_output(
        [
            "sudo",
            "-u",
            user_name,
            "docker",
            "login",
            "--username",
            "AWS",
            "--password",
            token,
            registry,
        ]
    )


def login_to_dockerhub(client: docker.DockerClient, user_name: str, uid: int) -> None:
    logger.info("Logging into Docker Hub")
    r = json.loads(
        subprocess.check_output(
            [
                "aws",
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                "docker_hub_readonly_token",
                "--region",
                "us-east-1",
            ],
            text=True,
        ).strip()
    )

    if "SecretString" not in r or not r["SecretString"]:
        logger.error(f"Fail to extract Docker Hub secret from {r}")
        return

    token = json.loads(r.get("SecretString")).get("docker_hub_readonly_token", "")
    response = client.login(username="pytorchbot", password=token)
    logger.info(response)

    # Write the credential to ~/.docker/config.json
    subprocess.check_output(
        [
            "sudo",
            "-u",
            user_name,
            "docker",
            "login",
            "--username",
            "pytorchbot",
            "--password",
            token,
        ]
    )


def get_github_app_client() -> Github:
    auth = Auth.AppAuth(get_github_app_id(), get_private_key()).get_installation_auth(
        get_github_app_installation_id()
    )
    return Github(auth=auth)


def get_gh_runner_token() -> str:
    gh_client = get_github_app_client()
    org = gh_client.get_organization("pytorch")
    _, data = org._requester.requestJsonAndCheck(
        "POST",
        f"/orgs/{org.login}/actions/runners/registration-token",
    )
    return data["token"]


def read_from_file(file_path: str) -> str:
    try:
        with open(file_path, "r") as f:
            return f.read().strip()
    except Exception as e:
        logger.info(f"Error reading file {file_path}: {str(e)}")
        raise


def get_instance_label_from_file() -> str:
    return read_from_file("/etc/gha-runner-config/instance-label")


def get_runner_url_from_file() -> str:
    return read_from_file("/etc/gha-runner-config/runner-url")


def get_private_key() -> str:
    return read_from_file("/etc/gha-runner-config/private-key")


def get_github_app_id() -> int:
    return int(read_from_file("/etc/gha-runner-config/app-id"))


def get_github_app_installation_id() -> int:
    return int(read_from_file("/etc/gha-runner-config/installation-id"))


def get_docker_tag_from_file() -> str:
    return read_from_file("/etc/gha-runner-config/image-tag-version")


def lock_nvidia_gpu_clock() -> None:
    with_b200 = False

    try:
        result = subprocess.run(["nvidia-smi"], stdout=subprocess.PIPE)
        if "B200" in result.stdout.decode():
            logger.info("Detected B200 GPU")
            with_b200 = True
    except Exception as e:
        logger.warning(f"Error checking GPU model: {str(e)}")

    if with_b200:
        logger.info("Locking GPU clock to 1620 MHz (B200)")
        try:
            subprocess.run(["nvidia-smi", "-pm", "1"], check=True)
            subprocess.run(["nvidia-smi", "-ac", "1620,1620"], check=True)
            subprocess.run(["nvidia-smi", "-pl", "750"], check=True)
        except Exception as e:
            logger.warning(f"Error locking GPU clock: {str(e)}")


def monitor_containers() -> None:
    logger.info("Starting container monitor on instance daemon...")

    uid_sockets_list = check_socket_for_users()
    user_name_print = ", ".join(
        [uid_socket.user_name for uid_socket in uid_sockets_list]
    )
    logger.info(f"Users to monitor: {user_name_print}")

    docker_group_id = grp.getgrnam("docker").gr_gid
    valid_uid_sockets_list = [
        uid_socket for uid_socket in uid_sockets_list if uid_socket.docker_client
    ]

    instance_label = get_instance_label_from_file()
    runner_url = get_runner_url_from_file()

    try:
        docker_tag = get_docker_tag_from_file()
    except Exception as e:
        docker_tag = DOCKER_TAG
        logger.warning(
            f"Error reading Docker tag from file: {str(e)}, using default {DOCKER_TAG}"
        )

    while True:
        for uid_sockets in valid_uid_sockets_list:
            logger.debug(f"Checking container for user {uid_sockets.user_name}")
            start_container_if_not_running(
                runner_url,
                instance_label,
                uid_sockets.uid,
                uid_sockets.docker_client,
                docker_group_id,
                REQUIRED_CONTAINER_NAME,
                uid_sockets.user_name,
                docker_tag,
            )
        logger.debug("Sleeping for 20 seconds")
        time.sleep(20)


if __name__ == "__main__":
    logging.basicConfig(
        filename="/var/log/ghad-manager.log",
        level=logging.INFO,
        format="%(asctime)s %(message)s",
    )
    lock_nvidia_gpu_clock()
    monitor_containers()
