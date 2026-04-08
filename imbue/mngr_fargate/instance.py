"""Fargate provider instance — manages ECS Fargate tasks as mngr hosts."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence
from uuid import uuid4

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.offline_host import OfflineHost
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.interfaces.data_types import CpuResources
from imbue.mngr.interfaces.data_types import HostLifecycleOptions
from imbue.mngr.interfaces.data_types import HostResources
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.interfaces.data_types import VolumeInfo
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostNameStyle
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ImageReference
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName
from imbue.mngr.primitives import VolumeId
from imbue.mngr.providers.base_provider import BaseProviderInstance
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_fargate.config import FargateProviderConfig
from imbue.mngr_fargate.ecs_client import FargateClient


def _scan_and_add_host_key(hostname: str, port: int, known_hosts_path: Path) -> None:
    """Use ssh-keyscan to get host keys and write them to known_hosts."""
    import subprocess

    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)

    port_arg = ["-p", str(port)] if port != 22 else []
    result = subprocess.run(
        ["ssh-keyscan", *port_arg, hostname],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip():
        with open(known_hosts_path, "a") as f:
            f.write(result.stdout)
        logger.debug("Added host keys for {} to {}", hostname, known_hosts_path)
    else:
        logger.warning("ssh-keyscan failed for {}:{}", hostname, port)


class FargateHostRecord:
    """Tracks a Fargate task and its associated mngr host state."""

    def __init__(
        self,
        host_id: str,
        host_name: str,
        task_arn: str,
        ssh_host: str,
        ssh_port: int,
        ssh_key_path: Path,
        certified_data: CertifiedHostData,
    ):
        self.host_id = host_id
        self.host_name = host_name
        self.task_arn = task_arn
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_key_path = ssh_key_path
        self.certified_data = certified_data


class FargateProviderInstance(BaseProviderInstance):
    """Provider instance that manages ECS Fargate tasks as mngr hosts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: FargateProviderConfig = Field(frozen=True, description="Fargate-specific configuration")
    fargate_client: FargateClient = Field(frozen=True, description="ECS Fargate client")
    boto_session: Any = Field(frozen=True, description="boto3 session", repr=False)

    # Cache of known hosts: host_id -> (Host | FargateHostRecord)
    _host_cache: dict[str, Host | FargateHostRecord] = PrivateAttr(default_factory=dict)
    _offline_cache: dict[str, OfflineHost] = PrivateAttr(default_factory=dict)

    # =========================================================================
    # Capability Properties
    # =========================================================================

    @property
    def supports_snapshots(self) -> bool:
        return False  # Fargate tasks are ephemeral

    @property
    def supports_shutdown_hosts(self) -> bool:
        return False  # Fargate tasks can't be stopped and resumed

    @property
    def supports_volumes(self) -> bool:
        return False  # No Docker volume management

    @property
    def supports_mutable_tags(self) -> bool:
        return False  # ECS tags are immutable after task launch

    def reset_caches(self) -> None:
        self._host_cache.clear()
        self._offline_cache.clear()

    # =========================================================================
    # Core Lifecycle Methods
    # =========================================================================

    def create_host(
        self,
        name: HostName,
        image: ImageReference | None = None,
        tags: Mapping[str, str] | None = None,
        build_args: Sequence[str] | None = None,
        start_args: Sequence[str] | None = None,
        lifecycle: HostLifecycleOptions | None = None,
        known_hosts: Sequence[str] | None = None,
        authorized_keys: Sequence[str] | None = None,
        snapshot: SnapshotName | None = None,
    ) -> Host:
        """Create and start a new Fargate task as a mngr host."""
        host_id = HostId(f"host-{uuid4().hex}")

        # Generate SSH keypair for this provider
        profile_dir = self.mngr_ctx.profile_dir / "fargate"
        profile_dir.mkdir(parents=True, exist_ok=True)
        ssh_key_path, ssh_pub_key = load_or_create_ssh_keypair(
            profile_dir / "fargate_ssh_key",
        )

        # Build env overrides for the task
        env: dict[str, str] = {
            "MNGR_HOST_ID": str(host_id),
            "MNGR_HOST_NAME": str(name),
            "MNGR_HOST_DIR": str(self.host_dir),
            "MNGR_PROVIDER": str(self.name),
        }

        # Inject the SSH public key so the container can set up authorized_keys
        env["MNGR_SSH_PUBLIC_KEY"] = ssh_pub_key

        if authorized_keys:
            env["MNGR_EXTRA_AUTHORIZED_KEYS"] = "\n".join(authorized_keys)

        # Launch the Fargate task
        task_arn = self.fargate_client.run_task(
            host_id=str(host_id),
            host_name=str(name),
            provider_name=str(self.name),
            env=env,
            image=str(image) if image else None,
            cpu=self.config.cpu,
            memory=self.config.memory,
        )

        # Wait for task to reach RUNNING and get its IP
        logger.info("Waiting for Fargate task {} to start...", task_arn)
        task_info = self.fargate_client.wait_for_running(task_arn)

        ssh_host = task_info.get("public_ip") or task_info.get("ip")
        if not ssh_host:
            self.fargate_client.stop_task(task_arn, reason="no IP assigned")
            raise MngrError(f"Fargate task {task_arn} has no IP address")

        ssh_port = 22

        # Wait for SSH to be ready
        logger.info("Waiting for SSH on {}:{}...", ssh_host, ssh_port)
        wait_for_sshd(ssh_host, ssh_port, timeout_seconds=120)

        # Scan host keys and add to known_hosts
        known_hosts_path = profile_dir / "known_hosts"
        _scan_and_add_host_key(ssh_host, ssh_port, known_hosts_path)

        # Create pyinfra host connector
        pyinfra_host = create_pyinfra_host(
            hostname=ssh_host,
            port=ssh_port,
            private_key_path=ssh_key_path,
            known_hosts_path=known_hosts_path,
        )

        # Build certified data
        activity_config = (lifecycle or HostLifecycleOptions()).to_activity_config(
            default_idle_timeout_seconds=self.config.default_idle_timeout,
            default_idle_mode=self.config.default_idle_mode,
            default_activity_sources=self.config.default_activity_sources,
        )
        now = datetime.now(timezone.utc)
        certified_data = CertifiedHostData(
            host_id=str(host_id),
            host_name=str(name),
            created_at=now,
            updated_at=now,
            idle_timeout_seconds=activity_config.idle_timeout_seconds,
            activity_sources=activity_config.activity_sources,
            user_tags=dict(tags) if tags else {},
            image=str(image) if image else None,
            plugin={"fargate": {"task_arn": task_arn}},
        )

        # Create the Host object (mngr's standard host implementation)
        host = Host(
            id=host_id,
            connector=PyinfraConnector(pyinfra_host),
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )

        # Initialize host directory and write certified data
        host.execute_idempotent_command(f"mkdir -p {self.host_dir}")
        host.set_certified_data(certified_data)

        # Record boot activity
        host.record_activity(ActivitySource.BOOT)

        # Cache the host
        self._host_cache[str(host_id)] = host

        logger.info(
            "Fargate host created: id={}, name={}, ip={}, task={}",
            host_id, name, ssh_host, task_arn,
        )

        return host

    def stop_host(
        self,
        host: HostInterface | HostId,
        create_snapshot: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Stop a Fargate task. Snapshots are not supported."""
        host_id = host.id if isinstance(host, HostInterface) else host
        record = self._get_record(host_id)

        task_arn = self._get_task_arn(host_id, record)
        if task_arn:
            self.fargate_client.stop_task(task_arn, reason="mngr stop")

        # Remove from cache
        self._host_cache.pop(str(host_id), None)

    def destroy_host(self, host: HostInterface | HostId) -> None:
        """Destroy a Fargate host by stopping the task."""
        self.stop_host(host, create_snapshot=False)

    def delete_host(self, host: HostInterface) -> None:
        """Delete all records for a destroyed host."""
        self._host_cache.pop(str(host.id), None)
        self._offline_cache.pop(str(host.id), None)

    def on_connection_error(self, host_id: HostId) -> None:
        """Handle connection errors by checking if the task is still running."""
        self._host_cache.pop(str(host_id), None)

    # =========================================================================
    # Discovery Methods
    # =========================================================================

    def get_host(self, host: HostId | HostName) -> HostInterface:
        """Retrieve a host by ID or name."""
        host_key = str(host)

        # Check cache first
        if host_key in self._host_cache:
            cached = self._host_cache[host_key]
            if isinstance(cached, Host):
                return cached

        # Check offline cache
        if host_key in self._offline_cache:
            return self._offline_cache[host_key]

        raise HostNotFoundError(f"Host {host} not found")

    def to_offline_host(self, host_id: HostId) -> OfflineHost:
        """Return an offline representation of a Fargate host."""
        if str(host_id) in self._offline_cache:
            return self._offline_cache[str(host_id)]
        raise HostNotFoundError(f"No offline data for host {host_id}")

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all Fargate tasks managed by this provider."""
        discovered: list[DiscoveredHost] = []

        tasks = self.fargate_client.list_tasks_by_tag(str(self.name))

        for task in tasks:
            tags = {t["key"]: t["value"] for t in task.get("tags", [])}
            host_id_str = tags.get("mngr-host-id", "")
            host_name_str = tags.get("mngr-host-name", "")

            if not host_id_str:
                continue

            host_id = HostId(host_id_str)
            host_name = HostName(host_name_str)

            discovered.append(
                DiscoveredHost(
                    host_id=host_id,
                    host_name=host_name,
                    provider_name=self.name,
                )
            )

            # Try to create host objects for running tasks
            task_status = task.get("lastStatus", "")
            if task_status == "RUNNING":
                self._try_cache_running_task(host_id, host_name, task)

        return discovered

    def get_host_resources(self, host: HostInterface) -> HostResources:
        """Get resource information for a Fargate host."""
        return HostResources(
            cpu=CpuResources(count=self.config.cpu // 1024 or 1),
            memory_gb=self.config.memory / 1024,
        )

    # =========================================================================
    # Snapshot Methods (not supported for Fargate)
    # =========================================================================

    def create_snapshot(self, host: HostInterface | HostId, name: SnapshotName | None = None) -> SnapshotId:
        raise MngrError("Snapshots are not supported for Fargate hosts")

    def list_snapshots(self, host: HostInterface | HostId) -> list[SnapshotInfo]:
        return []

    def delete_snapshot(self, host: HostInterface | HostId, snapshot_id: SnapshotId) -> None:
        raise MngrError("Snapshots are not supported for Fargate hosts")

    # =========================================================================
    # Volume Methods (not supported for Fargate)
    # =========================================================================

    def list_volumes(self) -> list[VolumeInfo]:
        return []

    def delete_volume(self, volume_id: VolumeId) -> None:
        raise MngrError("Volumes are not supported for Fargate hosts")

    # =========================================================================
    # Tag Methods
    # =========================================================================

    def get_host_tags(self, host: HostInterface | HostId) -> dict[str, str]:
        host_obj = self._resolve_host(host)
        return host_obj.get_tags()

    def set_host_tags(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("Fargate task tags are immutable after creation")

    def add_tags_to_host(self, host: HostInterface | HostId, tags: Mapping[str, str]) -> None:
        raise MngrError("Fargate task tags are immutable after creation")

    def remove_tags_from_host(self, host: HostInterface | HostId, keys: Sequence[str]) -> None:
        raise MngrError("Fargate task tags are immutable after creation")

    def rename_host(self, host: HostInterface | HostId, name: HostName) -> HostInterface:
        raise MngrError("Fargate hosts cannot be renamed (tags are immutable)")

    # =========================================================================
    # Connector
    # =========================================================================

    def get_connector(self, host: HostInterface | HostId) -> Any:
        host_obj = self._resolve_host(host)
        if isinstance(host_obj, OnlineHostInterface):
            return host_obj.connector.host
        raise MngrError("Cannot get connector for offline host")

    # =========================================================================
    # Internal Helpers
    # =========================================================================

    def _resolve_host(self, host: HostInterface | HostId) -> HostInterface:
        if isinstance(host, HostInterface):
            return host
        return self.get_host(host)

    def _get_record(self, host_id: HostId) -> Host | FargateHostRecord | None:
        return self._host_cache.get(str(host_id))

    def _get_task_arn(self, host_id: HostId, record: Any) -> str | None:
        if isinstance(record, FargateHostRecord):
            return record.task_arn
        if isinstance(record, Host):
            certified = record.get_certified_data()
            fargate_plugin = certified.plugin.get("fargate", {})
            return fargate_plugin.get("task_arn")
        return None

    def _try_cache_running_task(
        self,
        host_id: HostId,
        host_name: HostName,
        task: dict[str, Any],
    ) -> None:
        """Try to create and cache a Host object for a running Fargate task."""
        if str(host_id) in self._host_cache:
            return

        # Extract IP
        public_ip = None
        for attachment in task.get("attachments", []):
            if attachment.get("type") == "ElasticNetworkInterface":
                for detail in attachment.get("details", []):
                    if detail.get("name") == "networkInterfaceId":
                        eni_id = detail["value"]
                        eni_resp = self.fargate_client._ec2.describe_network_interfaces(
                            NetworkInterfaceIds=[eni_id]
                        )
                        for ni in eni_resp.get("NetworkInterfaces", []):
                            public_ip = ni.get("Association", {}).get("PublicIp")
                        break

        if not public_ip:
            # Try private IP
            for container in task.get("containers", []):
                for ni in container.get("networkInterfaces", []):
                    public_ip = ni.get("privateIpv4Address")
                    break

        if not public_ip:
            return

        # Check if we can connect
        profile_dir = self.mngr_ctx.profile_dir / "fargate"
        ssh_key_path = profile_dir / "fargate_ssh_key"
        if not ssh_key_path.exists():
            return

        known_hosts_path = profile_dir / "known_hosts"
        _scan_and_add_host_key(public_ip, 22, known_hosts_path)

        pyinfra_host = create_pyinfra_host(
            hostname=public_ip,
            port=22,
            private_key_path=ssh_key_path,
            known_hosts_path=known_hosts_path,
        )

        host = Host(
            id=host_id,
            connector=PyinfraConnector(pyinfra_host),
            provider_instance=self,
            mngr_ctx=self.mngr_ctx,
        )

        self._host_cache[str(host_id)] = host
