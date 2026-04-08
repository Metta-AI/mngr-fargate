#!/usr/bin/env python3
"""End-to-end test: launch a Fargate task, SSH in, run a command, tear down.

Uses the cogent cluster in the cogtainer account (815935788409) which already
has VPC, subnets, SGs, and a task definition with sshd.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import boto3


# -- Config --
ACCOUNT_ID = "815935788409"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/OrganizationAccountAccessRole"
SOURCE_PROFILE = "softmax-org"
REGION = "us-east-1"

CLUSTER = "cogent"
TASK_DEFINITION = "mngr-fargate-task"
CONTAINER_NAME = "agent"
SUBNETS = ["subnet-04b97bb9ee743c2cf", "subnet-00959fa27672ef8e9"]
SECURITY_GROUPS = ["sg-0f21b87d5e3c60a7a"]


def get_session() -> boto3.Session:
    source = boto3.Session(profile_name=SOURCE_PROFILE)
    sts = source.client("sts")
    creds = sts.assume_role(RoleArn=ROLE_ARN, RoleSessionName="mngr-fargate-e2e")["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=REGION,
    )


def generate_ssh_keypair(tmp: Path) -> tuple[Path, str]:
    key_path = tmp / "test_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
        check=True,
    )
    pub_key = (tmp / "test_key.pub").read_text().strip()
    return key_path, pub_key


def run_task(session: boto3.Session, ssh_pub_key: str) -> str:
    ecs = session.client("ecs")
    resp = ecs.run_task(
        cluster=CLUSTER,
        taskDefinition=TASK_DEFINITION,
        launchType="FARGATE",
        enableExecuteCommand=True,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": SUBNETS,
                "securityGroups": SECURITY_GROUPS,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": CONTAINER_NAME,
                    "environment": [
                        {"name": "MNGR_SSH_PUBLIC_KEY", "value": ssh_pub_key},
                        {"name": "MNGR_HOST_ID", "value": "e2e-test"},
                        {"name": "MNGR_HOST_DIR", "value": "/mngr"},
                    ],
                }
            ]
        },
        tags=[
            {"key": "mngr-provider", "value": "fargate-e2e-test"},
            {"key": "mngr-host-id", "value": "e2e-test"},
        ],
    )
    failures = resp.get("failures", [])
    if failures:
        print(f"FAIL: RunTask failed: {failures}")
        sys.exit(1)
    task_arn = resp["tasks"][0]["taskArn"]
    print(f"  Task ARN: {task_arn}")
    return task_arn


def wait_for_ip(session: boto3.Session, task_arn: str, timeout: int = 360) -> str:
    ecs = session.client("ecs")
    ec2 = session.client("ec2")
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        resp = ecs.describe_tasks(cluster=CLUSTER, tasks=[task_arn])
        task = resp["tasks"][0]
        status = task["lastStatus"]
        print(f"  Task status: {status}")

        if status == "STOPPED":
            reason = task.get("stoppedReason", "unknown")
            print(f"FAIL: Task stopped: {reason}")
            # Check container exit codes
            for c in task.get("containers", []):
                print(f"  Container {c['name']}: exit={c.get('exitCode')} reason={c.get('reason')}")
            sys.exit(1)

        if status == "RUNNING":
            # Get public IP from ENI
            for att in task.get("attachments", []):
                if att["type"] == "ElasticNetworkInterface":
                    eni_id = None
                    for d in att["details"]:
                        if d["name"] == "networkInterfaceId":
                            eni_id = d["value"]
                    if eni_id:
                        eni_resp = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
                        for ni in eni_resp["NetworkInterfaces"]:
                            public_ip = ni.get("Association", {}).get("PublicIp")
                            if public_ip:
                                return public_ip
            # Fallback to private IP
            for c in task.get("containers", []):
                for ni in c.get("networkInterfaces", []):
                    ip = ni.get("privateIpv4Address")
                    if ip:
                        print(f"  WARNING: no public IP, using private: {ip}")
                        return ip

        time.sleep(10)

    print("FAIL: Timeout waiting for task IP")
    sys.exit(1)


def wait_for_ssh(host: str, key_path: Path, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5",
                "-o", "BatchMode=yes",
                "-i", str(key_path),
                f"root@{host}",
                "echo", "ssh-ok",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and "ssh-ok" in result.stdout:
            return
        print(f"  SSH not ready yet (rc={result.returncode}){' stderr: ' + result.stderr.strip()[:100] if result.stderr.strip() else ''}")
        time.sleep(5)

    print("FAIL: SSH timeout")
    sys.exit(1)


def ssh_command(host: str, key_path: Path, cmd: str) -> str:
    user = "root"
    result = subprocess.run(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes",
            "-i", str(key_path),
            f"{user}@{host}",
            cmd,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"  SSH command failed: {result.stderr}")
    return result.stdout.strip()


def stop_task(session: boto3.Session, task_arn: str) -> None:
    ecs = session.client("ecs")
    ecs.stop_task(cluster=CLUSTER, task=task_arn, reason="e2e test cleanup")


def main() -> None:
    print("=== mngr-fargate e2e test ===")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 1. Generate SSH keypair
        print("[1/6] Generating SSH keypair...")
        key_path, pub_key = generate_ssh_keypair(tmp_path)

        # 2. Assume role and launch task
        print("[2/6] Launching Fargate task...")
        session = get_session()
        task_arn = run_task(session, pub_key)

        try:
            # 3. Wait for task to get an IP
            print("[3/6] Waiting for task IP...")
            ip = wait_for_ip(session, task_arn)
            print(f"  IP: {ip}")

            # 4. Wait for SSH
            print("[4/6] Waiting for SSH...")
            wait_for_ssh(ip, key_path)
            print("  SSH ready!")

            # 5. Run commands via SSH
            print("[5/6] Running commands via SSH...")

            hostname = ssh_command(ip, key_path, "hostname")
            print(f"  hostname: {hostname}")

            uname = ssh_command(ip, key_path, "uname -a")
            print(f"  uname: {uname}")

            # Check that mngr host directory can be created
            ssh_command(ip, key_path, "mkdir -p /tmp/mngr-test && echo ok > /tmp/mngr-test/test.txt")
            test_content = ssh_command(ip, key_path, "cat /tmp/mngr-test/test.txt")
            assert test_content == "ok", f"Expected 'ok', got '{test_content}'"
            print("  File write/read: ok")

            # Check git is available
            git_version = ssh_command(ip, key_path, "git --version")
            print(f"  git: {git_version}")

            # Check tmux is available
            tmux_version = ssh_command(ip, key_path, "tmux -V")
            print(f"  tmux: {tmux_version}")

            print()
            print("[6/6] All checks passed!")

        finally:
            # 6. Cleanup
            print()
            print("Stopping task...")
            stop_task(session, task_arn)
            print("Done.")


if __name__ == "__main__":
    main()
