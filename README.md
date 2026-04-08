# mngr-fargate

[mngr](https://github.com/imbue-ai/mngr) provider plugin for AWS ECS Fargate.

Launches AI coding agents in Fargate tasks with SSH access, following the same pattern as mngr's Docker and Modal providers.

## Install

```bash
uv pip install imbue-mngr-fargate
```

## Configuration

Add to `~/.mngr/config.toml`:

```toml
[providers.fargate]
backend = "fargate"
aws_region = "us-east-1"
ecs_cluster = "mngr"
task_definition = "mngr-task"
subnets = ["subnet-abc123", "subnet-def456"]
security_groups = ["sg-abc123"]
```

Or via environment variables:

```bash
export MNGR_FARGATE_SUBNETS="subnet-abc123,subnet-def456"
export MNGR_FARGATE_SECURITY_GROUPS="sg-abc123"
```

### Config options

| Option | Default | Description |
|--------|---------|-------------|
| `aws_region` | `us-east-1` | AWS region |
| `ecs_cluster` | `mngr` | ECS cluster name |
| `task_definition` | `mngr-task` | Task definition family |
| `subnets` | `[]` | VPC subnet IDs |
| `security_groups` | `[]` | Security group IDs |
| `assign_public_ip` | `true` | Assign public IP for SSH |
| `container_name` | `agent` | Container name in task def |
| `cpu` | `1024` | CPU units (256-4096) |
| `memory` | `4096` | Memory MiB |
| `aws_profile` | `null` | AWS profile for auth |
| `aws_role_arn` | `null` | IAM role to assume |

## AWS Setup

### Prerequisites

1. **ECS Cluster** — create one or use an existing cluster
2. **Task Definition** — Fargate task definition with:
   - SSH server (sshd) in the container
   - Port 22 exposed
   - `enableExecuteCommand` for ECS Exec fallback
3. **VPC** — subnets with internet access (public subnets or NAT gateway)
4. **Security Group** — allowing inbound TCP 22 (SSH)
5. **IAM** — permissions for `ecs:RunTask`, `ecs:StopTask`, `ecs:DescribeTasks`, `ec2:DescribeNetworkInterfaces`

### Task Definition Container Requirements

The container image must have:
- `sshd` installed and configured
- The `MNGR_SSH_PUBLIC_KEY` env var used to populate `~/.ssh/authorized_keys`
- Standard mngr host packages: `git`, `tmux`, `rsync`, `jq`, `curl`

See `docker/Dockerfile` for a reference image.

### CDK Example

```python
task_def = ecs.FargateTaskDefinition(
    self, "TaskDef",
    family="mngr-task",
    cpu=1024,
    memory_limit_mib=4096,
)

task_def.add_container("agent",
    image=ecs.ContainerImage.from_registry("your-image"),
    logging=ecs.LogDriver.aws_logs(stream_prefix="mngr"),
)

task_sg = ec2.SecurityGroup(self, "TaskSg", vpc=vpc, allow_all_outbound=True)
task_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22), "SSH")
```

## Usage

```bash
# Create an agent on Fargate
mngr create my-agent@.fargate

# List running agents
mngr list

# Connect to the agent
mngr connect my-agent

# Destroy
mngr destroy my-agent
```

## Limitations

- **No snapshots** — Fargate tasks are ephemeral; use EFS for persistent data
- **No stop/resume** — stopping a task destroys it; only create/destroy are supported
- **Immutable tags** — ECS task tags cannot be modified after launch
- **Startup time** — 30-60s cold start vs 2s for local provider
- **Cost** — billed per-second; configure idle timeout to avoid waste

## Development

```bash
git clone https://github.com/metta-ai/mngr-fargate
cd mngr-fargate
uv pip install -e ".[dev]"
```
