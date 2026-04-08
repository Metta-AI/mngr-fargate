#!/usr/bin/env python3
"""Interactive setup wizard for mngr-fargate.

Walks through AWS infrastructure discovery, image building, task definition
registration, and config generation. Queries AWS proactively and offers
choices where possible.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import boto3
import botocore.exceptions


# ── Helpers ──────────────────────────────────────────────────────────────────

def prompt(msg: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or (default or "")


def pick(label: str, options: list[tuple[str, str]], allow_custom: bool = False) -> str:
    """Let the user pick from a numbered list. Returns the value (first element)."""
    print(f"\n{label}")
    for i, (value, desc) in enumerate(options, 1):
        print(f"  {i}) {desc}")
    if allow_custom:
        print(f"  {len(options) + 1}) Enter custom value")

    while True:
        raw = input("Choice: ").strip()
        if not raw:
            continue
        try:
            idx = int(raw)
        except ValueError:
            continue
        if 1 <= idx <= len(options):
            return options[idx - 1][0]
        if allow_custom and idx == len(options) + 1:
            return input("Value: ").strip()
    return ""


def pick_multi(label: str, options: list[tuple[str, str]]) -> list[str]:
    """Let the user pick multiple items (comma-separated indices)."""
    print(f"\n{label}")
    for i, (value, desc) in enumerate(options, 1):
        print(f"  {i}) {desc}")
    print("Enter comma-separated numbers (e.g. 1,2):")

    while True:
        raw = input("Choice: ").strip()
        if not raw:
            continue
        try:
            indices = [int(x.strip()) for x in raw.split(",")]
            selected = [options[i - 1][0] for i in indices if 1 <= i <= len(options)]
            if selected:
                return selected
        except (ValueError, IndexError):
            continue


def heading(text: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}")


def yes_no(msg: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    val = input(f"{msg}{suffix}: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


# ── AWS Session ──────────────────────────────────────────────────────────────

def get_session_direct(region: str, profile: str | None) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def get_session_assume(region: str, profile: str | None, role_arn: str) -> boto3.Session:
    source = boto3.Session(profile_name=profile, region_name=region)
    sts = source.client("sts")
    creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="mngr-fargate-setup")["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def build_session() -> tuple[boto3.Session, str, str | None, str | None]:
    """Interactive session builder. Returns (session, region, profile, role_arn)."""
    heading("AWS Authentication")

    # List available profiles
    available_profiles = boto3.Session().available_profiles
    if available_profiles:
        profile_options = [(p, p) for p in sorted(available_profiles)]
        profile_options.insert(0, ("", "Default credentials (no profile)"))
        profile = pick("Select AWS profile:", profile_options)
        profile = profile or None
    else:
        print("No AWS profiles found, using default credentials.")
        profile = None

    region = prompt("AWS region", "us-east-1")

    # Test credentials
    try:
        test_session = boto3.Session(profile_name=profile, region_name=region)
        sts = test_session.client("sts")
        identity = sts.get_caller_identity()
        account = identity["Account"]
        arn = identity["Arn"]
        print(f"\n  Authenticated as: {arn}")
        print(f"  Account: {account}")
    except Exception as e:
        print(f"\n  Failed to authenticate: {e}")
        sys.exit(1)

    # Check if we need to assume a role (cross-account)
    role_arn = None
    if yes_no("\nDo you need to assume a role (cross-account)?", default=False):
        role_arn = prompt("IAM role ARN to assume")
        try:
            session = get_session_assume(region, profile, role_arn)
            identity = session.client("sts").get_caller_identity()
            print(f"  Assumed into account: {identity['Account']}")
            print(f"  As: {identity['Arn']}")
        except Exception as e:
            print(f"  Failed to assume role: {e}")
            sys.exit(1)
    else:
        session = test_session

    return session, region, profile, role_arn


# ── ECS Cluster ──────────────────────────────────────────────────────────────

def pick_or_create_cluster(session: boto3.Session) -> str:
    heading("ECS Cluster")
    ecs = session.client("ecs")

    clusters = ecs.list_clusters().get("clusterArns", [])
    if clusters:
        descs = ecs.describe_clusters(clusters=clusters)["clusters"]
        active = [(c["clusterName"], f"{c['clusterName']} ({c['runningTasksCount']} running tasks)")
                  for c in descs if c["status"] == "ACTIVE"]
    else:
        active = []

    if active:
        active.append(("__create__", "Create a new cluster"))
        choice = pick("Select ECS cluster:", active)
        if choice != "__create__":
            print(f"  Using cluster: {choice}")
            return choice

    name = prompt("New cluster name", "mngr")
    ecs.create_cluster(clusterName=name)
    print(f"  Created cluster: {name}")
    return name


# ── VPC / Subnets / Security Groups ─────────────────────────────────────────

def pick_vpc_and_networking(session: boto3.Session) -> tuple[list[str], list[str]]:
    heading("Networking")
    ec2 = session.client("ec2")

    # List VPCs
    vpcs = ec2.describe_vpcs()["Vpcs"]
    vpc_options = []
    for v in vpcs:
        name = next((t["Value"] for t in v.get("Tags", []) if t["Key"] == "Name"), "")
        label = f"{v['VpcId']} — {name}" if name else v["VpcId"]
        label += f" ({v['CidrBlock']})"
        if v.get("IsDefault"):
            label += " [default]"
        vpc_options.append((v["VpcId"], label))

    vpc_id = pick("Select VPC:", vpc_options)

    # List subnets in this VPC
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    subnet_options = []
    for s in subnets:
        name = next((t["Value"] for t in s.get("Tags", []) if t["Key"] == "Name"), "")
        public = "public" if s.get("MapPublicIpOnLaunch") else "private"
        label = f"{s['SubnetId']} — {s['AvailabilityZone']} ({public})"
        if name:
            label += f" [{name}]"
        subnet_options.append((s["SubnetId"], label))

    # Recommend public subnets
    public_subnets = [s for s in subnet_options if "public" in s[1]]
    if public_subnets:
        print("\n  Tip: Pick public subnets so tasks get public IPs for SSH.")

    selected_subnets = pick_multi("Select subnets (need at least 1):", subnet_options)

    # List security groups in this VPC
    sgs = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
    sg_options = []
    for sg in sgs:
        has_ssh = any(
            r.get("FromPort") == 22 or (r.get("FromPort", 0) <= 22 <= r.get("ToPort", 0))
            for r in sg.get("IpPermissions", [])
        )
        label = f"{sg['GroupId']} — {sg['GroupName']}"
        if has_ssh:
            label += " [has SSH]"
        sg_options.append((sg["GroupId"], label))

    # Check if any SG has SSH
    ssh_sgs = [s for s in sg_options if "has SSH" in s[1]]
    if not ssh_sgs:
        print("\n  Warning: No security groups with SSH (port 22) access found.")
        if yes_no("  Create one?"):
            sg_name = prompt("Security group name", "mngr-fargate-ssh")
            new_sg = ec2.create_security_group(
                GroupName=sg_name,
                Description="mngr-fargate — SSH access",
                VpcId=vpc_id,
            )
            sg_id = new_sg["GroupId"]
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH for mngr"}],
                }],
            )
            print(f"  Created security group: {sg_id} with SSH access")
            selected_sgs = [sg_id]
            return selected_subnets, selected_sgs

    selected_sgs = pick_multi("Select security groups:", sg_options)
    return selected_subnets, selected_sgs


# ── Container Image ──────────────────────────────────────────────────────────

def setup_image(session: boto3.Session, region: str) -> str:
    heading("Container Image")
    ecr = session.client("ecr")
    account = session.client("sts").get_caller_identity()["Account"]

    # Check for existing mngr-fargate repo
    try:
        ecr.describe_repositories(repositoryNames=["mngr-fargate"])
        repo_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/mngr-fargate"
        print(f"  ECR repo exists: {repo_uri}")

        # Check for images
        try:
            images = ecr.list_images(repositoryName="mngr-fargate", filter={"tagStatus": "TAGGED"})
            tags = [img.get("imageTag") for img in images.get("imageIds", []) if img.get("imageTag")]
            if tags:
                print(f"  Available tags: {', '.join(tags)}")
        except Exception:
            tags = []

    except ecr.exceptions.RepositoryNotFoundException:
        repo_uri = None
        tags = []

    options = []
    if repo_uri and "latest" in tags:
        options.append(("existing", f"Use existing image: {repo_uri}:latest"))
    options.append(("build", "Build and push the mngr-fargate image now"))
    options.append(("custom", "Enter a custom image URI"))

    choice = pick("Container image:", options)

    if choice == "existing":
        return f"{repo_uri}:latest"

    if choice == "custom":
        return prompt("Image URI")

    # Build and push
    if not repo_uri:
        print("  Creating ECR repository...")
        ecr.create_repository(repositoryName="mngr-fargate")
        repo_uri = f"{account}.dkr.ecr.{region}.amazonaws.com/mngr-fargate"
        print(f"  Created: {repo_uri}")

    # Docker login
    print("  Logging into ECR...")
    token = ecr.get_authorization_token()["authorizationData"][0]
    decoded = base64.b64decode(token["authorizationToken"]).decode()
    username, password = decoded.split(":", 1)
    subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", token["proxyEndpoint"]],
        input=password, capture_output=True, text=True, check=True,
    )

    # Build
    dockerfile_dir = Path(__file__).parent / "docker"
    image_tag = f"{repo_uri}:latest"
    print(f"  Building image: {image_tag}")
    result = subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "-t", image_tag, str(dockerfile_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  Build failed:\n{result.stderr[-500:]}")
        sys.exit(1)
    print("  Build complete.")

    # Push
    print("  Pushing to ECR...")
    result = subprocess.run(["docker", "push", image_tag], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  Push failed:\n{result.stderr[-500:]}")
        sys.exit(1)
    print("  Push complete.")

    return image_tag


# ── Task Definition ──────────────────────────────────────────────────────────

def pick_or_create_task_def(
    session: boto3.Session, cluster: str, image_uri: str
) -> tuple[str, str]:
    """Returns (task_definition_family, container_name)."""
    heading("Task Definition")
    ecs = session.client("ecs")

    # List existing task definitions
    families: list[str] = []
    paginator = ecs.get_paginator("list_task_definition_families")
    for page in paginator.paginate(status="ACTIVE"):
        families.extend(page.get("families", []))

    options = []
    for fam in sorted(families):
        options.append((fam, fam))
    options.append(("__create__", "Register a new task definition"))

    choice = pick("Select task definition:", options)

    if choice != "__create__":
        # Get container name from the task def
        td = ecs.describe_task_definition(taskDefinition=choice)["taskDefinition"]
        containers = td.get("containerDefinitions", [])
        if len(containers) == 1:
            container_name = containers[0]["name"]
        else:
            container_opts = [(c["name"], f"{c['name']} — {c.get('image', '?')}") for c in containers]
            container_name = pick("Which container?", container_opts)
        print(f"  Using: {choice} (container: {container_name})")
        return choice, container_name

    # Register new task definition
    family = prompt("Task definition family name", "mngr-fargate-task")
    cpu = prompt("CPU units (256/512/1024/2048/4096)", "1024")
    memory = prompt("Memory MiB", "4096")

    # Find execution and task roles
    iam_client = session.client("iam")
    print("\n  Looking for IAM roles...")

    exec_roles = []
    task_roles = []
    paginator = iam_client.get_paginator("list_roles")
    for page in paginator.paginate():
        for role in page["Roles"]:
            trust = json.dumps(role.get("AssumeRolePolicyDocument", {}))
            if "ecs-tasks.amazonaws.com" in trust:
                name = role["RoleName"]
                arn = role["Arn"]
                # Heuristic: execution roles usually have "Execution" in name
                if "execution" in name.lower() or "exec" in name.lower():
                    exec_roles.append((arn, name))
                else:
                    task_roles.append((arn, name))

    # If no clear split, put all in both
    all_ecs_roles = exec_roles + task_roles
    if not exec_roles:
        exec_roles = all_ecs_roles
    if not task_roles:
        task_roles = all_ecs_roles

    if exec_roles:
        exec_role_arn = pick("Execution role (pulls images, writes logs):", exec_roles, allow_custom=True)
    else:
        exec_role_arn = prompt("Execution role ARN")

    if task_roles:
        task_role_arn = pick("Task role (runtime permissions):", task_roles, allow_custom=True)
    else:
        task_role_arn = prompt("Task role ARN")

    # Find a log group
    logs_client = session.client("logs")
    log_groups = logs_client.describe_log_groups(limit=50).get("logGroups", [])
    lg_options = [(lg["logGroupName"], lg["logGroupName"]) for lg in log_groups]
    lg_options.append(("__create__", "Create /mngr/fargate"))

    log_group = pick("Log group:", lg_options)
    if log_group == "__create__":
        log_group = "/mngr/fargate"
        logs_client.create_log_group(logGroupName=log_group)
        print(f"  Created log group: {log_group}")

    container_name = "agent"

    print(f"\n  Registering task definition: {family}")
    ecs.register_task_definition(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu=cpu,
        memory=memory,
        executionRoleArn=exec_role_arn,
        taskRoleArn=task_role_arn,
        containerDefinitions=[{
            "name": container_name,
            "image": image_uri,
            "essential": True,
            "portMappings": [{"containerPort": 22, "protocol": "tcp"}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": log_group,
                    "awslogs-region": session.region_name,
                    "awslogs-stream-prefix": "mngr-fargate",
                },
            },
        }],
    )
    print(f"  Registered: {family}")
    return family, container_name


# ── Config Generation ────────────────────────────────────────────────────────

def write_config(
    region: str,
    profile: str | None,
    role_arn: str | None,
    cluster: str,
    task_def: str,
    container_name: str,
    subnets: list[str],
    security_groups: list[str],
) -> None:
    heading("Configuration")

    subnets_toml = ", ".join(f'"{s}"' for s in subnets)
    sgs_toml = ", ".join(f'"{s}"' for s in security_groups)

    config_block = f"""\
[providers.fargate]
backend = "fargate"
aws_region = "{region}"
ecs_cluster = "{cluster}"
task_definition = "{task_def}"
container_name = "{container_name}"
subnets = [{subnets_toml}]
security_groups = [{sgs_toml}]"""

    if role_arn:
        config_block += f'\naws_role_arn = "{role_arn}"'
    if profile:
        config_block += f'\naws_profile = "{profile}"'

    print("\nGenerated config:\n")
    print(config_block)

    config_path = Path.home() / ".mngr" / "config.toml"
    if config_path.exists():
        existing = config_path.read_text()
        if "[providers.fargate]" in existing:
            print(f"\n  Warning: {config_path} already has a [providers.fargate] section.")
            if yes_no("  Overwrite it?"):
                # Remove old section
                lines = existing.split("\n")
                new_lines = []
                skip = False
                for line in lines:
                    if line.strip() == "[providers.fargate]":
                        skip = True
                        continue
                    if skip and line.strip().startswith("["):
                        skip = False
                    if not skip:
                        new_lines.append(line)
                existing = "\n".join(new_lines).rstrip() + "\n"
            else:
                print(f"\n  Config not written. Add manually to {config_path}")
                return
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""

    with open(config_path, "a" if existing and not existing.endswith("\n\n") else "w") as f:
        if existing:
            f.write(existing.rstrip() + "\n\n")
        f.write(config_block + "\n")

    print(f"\n  Written to {config_path}")


# ── Smoke Test ───────────────────────────────────────────────────────────────

def smoke_test(
    session: boto3.Session,
    cluster: str,
    task_def: str,
    container_name: str,
    subnets: list[str],
    security_groups: list[str],
) -> None:
    heading("Smoke Test")

    if not yes_no("Launch a test task to verify everything works?"):
        return

    import tempfile

    ecs = session.client("ecs")
    ec2 = session.client("ec2")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Generate SSH key
        key_path = tmp_path / "test_key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-q"],
            check=True,
        )
        pub_key = (tmp_path / "test_key.pub").read_text().strip()

        # Launch task
        print("  Launching task...")
        resp = ecs.run_task(
            cluster=cluster,
            taskDefinition=task_def,
            launchType="FARGATE",
            enableExecuteCommand=True,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnets,
                    "securityGroups": security_groups,
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name": container_name,
                    "environment": [
                        {"name": "MNGR_SSH_PUBLIC_KEY", "value": pub_key},
                    ],
                }]
            },
            tags=[{"key": "mngr-provider", "value": "fargate-smoke-test"}],
        )

        failures = resp.get("failures", [])
        if failures:
            print(f"  FAIL: {failures}")
            return

        task_arn = resp["tasks"][0]["taskArn"]
        short_id = task_arn.split("/")[-1][:12]
        print(f"  Task: {short_id}...")

        try:
            # Wait for RUNNING + IP
            import time
            ip = None
            for _ in range(36):  # 3 minutes
                desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
                task = desc["tasks"][0]
                status = task["lastStatus"]

                if status == "STOPPED":
                    reason = task.get("stoppedReason", "unknown")
                    for c in task.get("containers", []):
                        if c.get("reason"):
                            reason += f" | {c['name']}: {c['reason']}"
                    print(f"  FAIL: Task stopped — {reason}")
                    return

                if status == "RUNNING":
                    for att in task.get("attachments", []):
                        if att["type"] == "ElasticNetworkInterface":
                            eni_id = next(
                                (d["value"] for d in att["details"] if d["name"] == "networkInterfaceId"),
                                None,
                            )
                            if eni_id:
                                eni = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
                                ip = eni["NetworkInterfaces"][0].get("Association", {}).get("PublicIp")
                    if not ip:
                        for c in task.get("containers", []):
                            for ni in c.get("networkInterfaces", []):
                                ip = ni.get("privateIpv4Address")
                    if ip:
                        break

                sys.stdout.write(f"\r  Waiting... ({status})")
                sys.stdout.flush()
                time.sleep(5)

            print()

            if not ip:
                print("  FAIL: No IP address assigned")
                return

            print(f"  IP: {ip}")

            # Wait for SSH
            print("  Waiting for SSH...")
            for _ in range(24):  # 2 minutes
                result = subprocess.run(
                    [
                        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                        "-i", str(key_path), f"root@{ip}", "echo", "ok",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and "ok" in result.stdout:
                    break
                time.sleep(5)
            else:
                print("  FAIL: SSH timeout")
                return

            # Run checks
            checks = [
                ("hostname", "hostname"),
                ("git", "git --version"),
                ("tmux", "tmux -V"),
            ]
            all_ok = True
            for name, cmd in checks:
                result = subprocess.run(
                    [
                        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "BatchMode=yes", "-i", str(key_path), f"root@{ip}", cmd,
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    print(f"  {name}: {result.stdout.strip()}")
                else:
                    print(f"  {name}: FAIL")
                    all_ok = False

            if all_ok:
                print("\n  All checks passed!")
            else:
                print("\n  Some checks failed.")

        finally:
            print("  Cleaning up...")
            ecs.stop_task(cluster=cluster, task=task_arn, reason="smoke test")
            print("  Done.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("""
    ┌──────────────────────────────────────┐
    │     mngr-fargate setup wizard        │
    └──────────────────────────────────────┘
    """)

    # 1. AWS auth
    session, region, profile, role_arn = build_session()

    # 2. Cluster
    cluster = pick_or_create_cluster(session)

    # 3. Networking
    subnets, security_groups = pick_vpc_and_networking(session)

    # 4. Container image
    image_uri = setup_image(session, region)

    # 5. Task definition
    task_def, container_name = pick_or_create_task_def(session, cluster, image_uri)

    # 6. Write config
    write_config(region, profile, role_arn, cluster, task_def, container_name, subnets, security_groups)

    # 7. Smoke test
    smoke_test(session, cluster, task_def, container_name, subnets, security_groups)

    heading("Done!")
    print("""
  You're all set. Try:

    mngr create my-agent@.fargate
    mngr connect my-agent
    mngr destroy my-agent
    """)


if __name__ == "__main__":
    main()
