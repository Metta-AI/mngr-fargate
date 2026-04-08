"""Configuration for the Fargate provider backend."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import IdleMode
from imbue.mngr.primitives import ProviderBackendName


class FargateProviderConfig(ProviderInstanceConfig):
    """Configuration for the Fargate provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("fargate"),
        description="Provider backend (always 'fargate' for this type)",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data inside tasks (defaults to /mngr)",
    )
    aws_region: str = Field(
        default="us-east-1",
        description="AWS region for ECS operations",
    )
    ecs_cluster: str = Field(
        default="mngr",
        description="ECS cluster name",
    )
    task_definition: str = Field(
        default="mngr-task",
        description="ECS task definition family name",
    )
    subnets: list[str] = Field(
        default_factory=list,
        description="VPC subnet IDs for Fargate tasks",
    )
    security_groups: list[str] = Field(
        default_factory=list,
        description="Security group IDs for Fargate tasks",
    )
    assign_public_ip: bool = Field(
        default=True,
        description="Whether to assign a public IP to Fargate tasks",
    )
    container_name: str = Field(
        default="agent",
        description="Container name within the task definition",
    )
    cpu: int = Field(
        default=1024,
        description="Fargate task CPU units (256, 512, 1024, 2048, 4096)",
    )
    memory: int = Field(
        default=4096,
        description="Fargate task memory in MiB",
    )
    default_image: str | None = Field(
        default=None,
        description="Default container image. None uses the task definition default.",
    )
    aws_profile: str | None = Field(
        default=None,
        description="AWS profile name for authentication. None uses default credentials.",
    )
    aws_role_arn: str | None = Field(
        default=None,
        description="IAM role ARN to assume for ECS operations. None uses direct credentials.",
    )
    default_idle_timeout: int = Field(
        default=3600,
        description="Default host idle timeout in seconds",
    )
    default_idle_mode: IdleMode = Field(
        default=IdleMode.IO,
        description="Default idle mode for hosts",
    )
    default_activity_sources: tuple[ActivitySource, ...] = Field(
        default_factory=lambda: (
            ActivitySource.USER,
            ActivitySource.AGENT,
            ActivitySource.SSH,
            ActivitySource.CREATE,
            ActivitySource.START,
            ActivitySource.BOOT,
        ),
        description="Default activity sources that count toward keeping host active",
    )

    def get_subnets(self) -> list[str]:
        """Get subnets from config or MNGR_FARGATE_SUBNETS env var."""
        if self.subnets:
            return self.subnets
        env_val = os.environ.get("MNGR_FARGATE_SUBNETS", "")
        return [s.strip() for s in env_val.split(",") if s.strip()]

    def get_security_groups(self) -> list[str]:
        """Get security groups from config or MNGR_FARGATE_SECURITY_GROUPS env var."""
        if self.security_groups:
            return self.security_groups
        env_val = os.environ.get("MNGR_FARGATE_SECURITY_GROUPS", "")
        return [s.strip() for s in env_val.split(",") if s.strip()]
