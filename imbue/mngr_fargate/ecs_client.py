"""Low-level ECS Fargate client — wraps boto3 for task lifecycle operations."""

from __future__ import annotations

from typing import Any

import boto3
from loguru import logger


class FargateClient:
    """Thin wrapper around boto3 ECS and EC2 clients for Fargate task operations."""

    def __init__(
        self,
        cluster: str,
        task_definition: str,
        subnets: list[str],
        security_groups: list[str],
        container_name: str = "agent",
        assign_public_ip: bool = True,
        session: boto3.Session | None = None,
    ):
        _session = session or boto3.Session()
        self._ecs = _session.client("ecs")
        self._ec2 = _session.client("ec2")
        self._cluster = cluster
        self._task_definition = task_definition
        self._subnets = subnets
        self._security_groups = security_groups
        self._container_name = container_name
        self._assign_public_ip = assign_public_ip

    def run_task(
        self,
        host_id: str,
        host_name: str,
        provider_name: str,
        env: dict[str, str] | None = None,
        image: str | None = None,
        cpu: int | None = None,
        memory: int | None = None,
    ) -> str:
        """Launch a Fargate task and return its ARN."""
        overrides: dict[str, Any] = {}
        container_overrides: dict[str, Any] = {"name": self._container_name}

        if env:
            container_overrides["environment"] = [
                {"name": k, "value": v} for k, v in env.items()
            ]

        if image:
            # Note: container image override requires registering a new task
            # definition revision. For now, we use env vars to signal the image.
            pass

        if cpu:
            overrides["cpu"] = str(cpu)
        if memory:
            overrides["memory"] = str(memory)

        overrides["containerOverrides"] = [container_overrides]

        tags = [
            {"key": "mngr-provider", "value": provider_name},
            {"key": "mngr-host-id", "value": host_id},
            {"key": "mngr-host-name", "value": host_name},
        ]

        logger.info("Launching Fargate task: cluster={}, host_id={}", self._cluster, host_id)

        resp = self._ecs.run_task(
            cluster=self._cluster,
            taskDefinition=self._task_definition,
            launchType="FARGATE",
            enableExecuteCommand=True,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": self._subnets,
                    "securityGroups": self._security_groups,
                    "assignPublicIp": "ENABLED" if self._assign_public_ip else "DISABLED",
                }
            },
            overrides=overrides,
            tags=tags,
        )

        failures = resp.get("failures", [])
        if failures:
            reasons = ", ".join(f["reason"] for f in failures)
            raise RuntimeError(f"Fargate RunTask failed: {reasons}")

        task_arn = resp["tasks"][0]["taskArn"]
        logger.info("Fargate task launched: {}", task_arn)
        return task_arn

    def stop_task(self, task_arn: str, reason: str = "mngr stop") -> None:
        """Stop a running Fargate task."""
        logger.info("Stopping Fargate task: {}", task_arn)
        self._ecs.stop_task(
            cluster=self._cluster,
            task=task_arn,
            reason=reason,
        )

    def describe_task(self, task_arn: str) -> dict[str, Any]:
        """Describe a Fargate task, returning status and IP information."""
        resp = self._ecs.describe_tasks(cluster=self._cluster, tasks=[task_arn])
        if not resp.get("tasks"):
            return {"task_arn": task_arn, "status": "UNKNOWN", "ip": None, "public_ip": None}

        task = resp["tasks"][0]
        private_ip = None
        public_ip = None

        # Private IP from container network interfaces
        for container in task.get("containers", []):
            for ni in container.get("networkInterfaces", []):
                private_ip = ni.get("privateIpv4Address")
                break

        # Public IP from the ENI attachment
        for attachment in task.get("attachments", []):
            if attachment.get("type") == "ElasticNetworkInterface":
                eni_id = None
                for detail in attachment.get("details", []):
                    if detail.get("name") == "networkInterfaceId":
                        eni_id = detail["value"]
                        break
                if eni_id:
                    eni_resp = self._ec2.describe_network_interfaces(
                        NetworkInterfaceIds=[eni_id]
                    )
                    for ni in eni_resp.get("NetworkInterfaces", []):
                        assoc = ni.get("Association", {})
                        public_ip = assoc.get("PublicIp")
                break

        return {
            "task_arn": task["taskArn"],
            "status": task["lastStatus"],
            "desired_status": task.get("desiredStatus"),
            "ip": private_ip,
            "public_ip": public_ip,
            "started_at": task.get("startedAt"),
            "stopped_at": task.get("stoppedAt"),
            "stopped_reason": task.get("stoppedReason"),
        }

    def list_tasks_by_tag(self, provider_name: str) -> list[dict[str, Any]]:
        """List all Fargate tasks tagged with our provider name."""
        task_arns: list[str] = []

        paginator = self._ecs.get_paginator("list_tasks")
        for page in paginator.paginate(
            cluster=self._cluster,
            desiredStatus="RUNNING",
        ):
            task_arns.extend(page.get("taskArns", []))

        if not task_arns:
            return []

        # Describe in batches of 100 (ECS limit)
        all_tasks: list[dict[str, Any]] = []
        for i in range(0, len(task_arns), 100):
            batch = task_arns[i : i + 100]
            resp = self._ecs.describe_tasks(
                cluster=self._cluster,
                tasks=batch,
                include=["TAGS"],
            )
            for task in resp.get("tasks", []):
                tags = {t["key"]: t["value"] for t in task.get("tags", [])}
                if tags.get("mngr-provider") == provider_name:
                    all_tasks.append(task)

        return all_tasks

    def wait_for_running(self, task_arn: str, timeout_seconds: int = 120) -> dict[str, Any]:
        """Wait for a task to reach RUNNING status and return its info."""
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            info = self.describe_task(task_arn)
            status = info["status"]
            if status == "RUNNING":
                return info
            if status in ("STOPPED", "DEPROVISIONING"):
                reason = info.get("stopped_reason", "unknown")
                raise RuntimeError(f"Fargate task stopped before reaching RUNNING: {reason}")
            logger.debug("Waiting for task {} to start (status: {})", task_arn, status)
            time.sleep(5)
        raise TimeoutError(f"Fargate task {task_arn} did not reach RUNNING within {timeout_seconds}s")
