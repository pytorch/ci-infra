import argparse
import json
import uuid
import itertools
from dataclasses import dataclass
from random import randint

import boto3


@dataclass
class ScaleQueue:
    installation_id: str
    queue_name: str
    region: str


SUPPORTED_SCALE_QUEUES = {
    "pytorch/pytorch": ScaleQueue(
        installation_id="51323823",
        queue_name="ghci-lf-queued-builds",
        region="us-east-1",
    ),
    "pytorch/pytorch-canary": ScaleQueue(
        installation_id="51321276",
        queue_name="ghci-lf-c-queued-builds",
        region="us-east-1",
    ),
}


def send_scale_message(queue, repository: str, runner_type: str, installation_id: str):
    message_body = json.dumps(
        {
            "id": str(uuid.uuid4()),
            "eventType": "workflow_job",
            "repositoryName": repository.split("/")[1],
            "repositoryOwner": repository.split("/")[0],
            "installationId": installation_id,
            "runnerLabels": [runner_type],
            "callbackUrl": "MANUAL SCALE EVENT",
        }
    )
    print(f"Sending message: {message_body}")
    queue.send_message(
        MessageBody=message_body,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clear offline self hosted runners for Github repositories"
    )
    parser.add_argument(
        "repo",
        help="Repository to remove offline self hosted runners for, (ex. pytorch/pytorch)",
        type=str,
        choices=SUPPORTED_SCALE_QUEUES.keys(),
    )
    parser.add_argument(
        "runner_type",
        help="Runner type to scale for",
        type=str,
    )
    parser.add_argument(
        "--scale-by",
        help="Number of scale messages to send",
        type=int,
        default=10,
    )
    options = parser.parse_args()
    return options


def main():
    options = parse_args()
    scale_queue_info = SUPPORTED_SCALE_QUEUES.get(options.repo)
    if scale_queue_info is None:
        exit(1)
    sqs = boto3.resource("sqs", region_name=scale_queue_info.region)
    queue = sqs.get_queue_by_name(QueueName=scale_queue_info.queue_name)
    for _ in range(options.scale_by):
        send_scale_message(
            queue=queue,
            repository=options.repo,
            runner_type=options.runner_type,
            installation_id=scale_queue_info.installation_id,
        )


if __name__ == "__main__":
    main()
