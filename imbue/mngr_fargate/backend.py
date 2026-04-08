"""Fargate provider backend — registers the plugin with mngr."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import boto3
from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_fargate import hookimpl
from imbue.mngr_fargate.config import FargateProviderConfig
from imbue.mngr_fargate.ecs_client import FargateClient
from imbue.mngr_fargate.instance import FargateProviderInstance

FARGATE_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("fargate")


def _build_boto_session(config: FargateProviderConfig) -> boto3.Session:
    """Build a boto3 session from config, with optional role assumption."""
    if config.aws_role_arn:
        source_session = boto3.Session(
            profile_name=config.aws_profile,
            region_name=config.aws_region,
        )
        sts = source_session.client("sts")
        creds = sts.assume_role(
            RoleArn=config.aws_role_arn,
            RoleSessionName="mngr-fargate",
        )["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=config.aws_region,
        )

    return boto3.Session(
        profile_name=config.aws_profile,
        region_name=config.aws_region,
    )


class FargateProviderBackend(ProviderBackendInterface):
    """Backend for creating Fargate provider instances.

    Launches agents in ECS Fargate tasks with SSH access. Each task runs
    sshd and is accessed via SSH/pyinfra, following the same pattern as
    the Docker and Modal providers.
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return FARGATE_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in AWS ECS Fargate tasks with SSH access"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return FargateProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return """\
Supported build arguments for the fargate provider:
  --cpu UNITS           CPU units (256, 512, 1024, 2048, 4096). Default: 1024
  --memory MIB          Memory in MiB (512-30720). Default: 4096
  --image URI           Container image URI. Default: task definition default
  --cluster NAME        ECS cluster name. Default: mngr
  --task-definition FAM Task definition family. Default: mngr-task
"""

    @staticmethod
    def get_start_args_help() -> str:
        return "No start arguments are supported for the fargate provider."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        """Build a Fargate provider instance."""
        if not isinstance(config, FargateProviderConfig):
            raise MngrError(f"Expected FargateProviderConfig, got {type(config).__name__}")

        host_dir = config.host_dir if config.host_dir is not None else Path("/mngr")

        session = _build_boto_session(config)

        subnets = config.get_subnets()
        security_groups = config.get_security_groups()

        if not subnets:
            raise MngrError(
                "No subnets configured for Fargate provider. "
                "Set 'subnets' in config or MNGR_FARGATE_SUBNETS env var."
            )

        fargate_client = FargateClient(
            cluster=config.ecs_cluster,
            task_definition=config.task_definition,
            subnets=subnets,
            security_groups=security_groups,
            container_name=config.container_name,
            assign_public_ip=config.assign_public_ip,
            session=session,
        )

        return FargateProviderInstance(
            name=name,
            host_dir=host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            fargate_client=fargate_client,
            boto_session=session,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the Fargate provider backend."""
    return (FargateProviderBackend, FargateProviderConfig)
