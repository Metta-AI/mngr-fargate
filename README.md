# mngr-fargate

[mngr](https://github.com/imbue-ai/mngr) provider plugin for AWS ECS Fargate.

Launches AI coding agents in Fargate tasks with SSH access, following the same pattern as mngr's Docker and Modal providers.

## Quick Start

```bash
# Install
cd mngr-fargate
uv pip install -e .

# Configure (~/.mngr/config.toml)
[providers.fargate]
backend = "fargate"
aws_region = "us-east-1"
ecs_cluster = "cogent"
task_definition = "mngr-fargate-task"
subnets = ["subnet-04b97bb9ee743c2cf", "subnet-00959fa27672ef8e9"]
security_groups = ["sg-0f21b87d5e3c60a7a"]
aws_role_arn = "arn:aws:iam::815935788409:role/OrganizationAccountAccessRole"
aws_profile = "softmax-org"

# Use
mngr create my-agent@.fargate
mngr connect my-agent
mngr destroy my-agent
```

## Configuration

Add a `[providers.fargate]` section to `~/.mngr/config.toml`:

```toml
[providers.fargate]
backend = "fargate"
aws_region = "us-east-1"
ecs_cluster = "cogent"
task_definition = "mngr-fargate-task"
subnets = ["subnet-04b97bb9ee743c2cf", "subnet-00959fa27672ef8e9"]
security_groups = ["sg-0f21b87d5e3c60a7a"]
aws_role_arn = "arn:aws:iam::815935788409:role/OrganizationAccountAccessRole"
aws_profile = "softmax-org"
```

Subnets and security groups can also be set via environment variables:

```bash
export MNGR_FARGATE_SUBNETS="subnet-04b97bb9ee743c2cf,subnet-00959fa27672ef8e9"
export MNGR_FARGATE_SECURITY_GROUPS="sg-0f21b87d5e3c60a7a"
```

### All Config Options

| Option | Default | Description |
|--------|---------|-------------|
| `aws_region` | `us-east-1` | AWS region |
| `ecs_cluster` | `mngr` | ECS cluster name |
| `task_definition` | `mngr-task` | Task definition family |
| `subnets` | `[]` | VPC subnet IDs (public, for SSH access) |
| `security_groups` | `[]` | Security group IDs (must allow inbound TCP 22) |
| `assign_public_ip` | `true` | Assign public IP to tasks |
| `container_name` | `agent` | Container name within the task definition |
| `cpu` | `1024` | CPU units (256, 512, 1024, 2048, 4096) |
| `memory` | `4096` | Memory in MiB |
| `aws_profile` | `null` | AWS profile for authentication |
| `aws_role_arn` | `null` | IAM role ARN to assume (cross-account) |
| `default_idle_timeout` | `3600` | Idle timeout in seconds before auto-stop |

## Usage

```bash
# Create an agent on Fargate
mngr create my-agent@.fargate

# List running agents
mngr list

# Connect via SSH
mngr connect my-agent

# Send a message
mngr message my-agent "fix the bug in auth.py"

# Destroy
mngr destroy my-agent
```

## AWS Infrastructure

The plugin needs:

1. **ECS Cluster** with Fargate capacity
2. **Task Definition** referencing a container image with sshd
3. **VPC** with public subnets (for SSH via public IP)
4. **Security Group** allowing inbound TCP 22
5. **IAM Roles** — execution role (pull images, write logs) and task role (runtime permissions)

### Using the CDK Stack

Deploy the included CDK stack to create all infrastructure:

```bash
cd infra
pip install aws-cdk-lib constructs
cdk deploy --context account=815935788409 --context region=us-east-1
```

The stack outputs the subnet IDs, security group ID, cluster name, and task definition family — paste them into your config.

### Using Existing Infrastructure

The Softmax cogtainer account (`815935788409`) already has compatible infrastructure. The config in Quick Start above uses the cogent cluster's VPC, subnets, and security group with a dedicated `mngr-fargate-task` task definition.

### Container Image

The `docker/` directory contains a minimal Dockerfile (debian + sshd + git + tmux + rsync). The image is pre-built at:

```
815935788409.dkr.ecr.us-east-1.amazonaws.com/mngr-fargate:latest
```

The container reads `MNGR_SSH_PUBLIC_KEY` from the environment to populate `~/.ssh/authorized_keys`, then runs sshd in the foreground.

To build and push a custom image:

```bash
docker build --platform linux/amd64 -t 815935788409.dkr.ecr.us-east-1.amazonaws.com/mngr-fargate:latest docker/
docker push 815935788409.dkr.ecr.us-east-1.amazonaws.com/mngr-fargate:latest
```

## How It Works

1. `mngr create` calls `FargateProviderInstance.create_host()` which:
   - Generates an SSH keypair (stored in `~/.mngr/profiles/<id>/fargate/`)
   - Launches an ECS Fargate task via `ecs:RunTask` with the SSH public key as an env var
   - Waits for the task to reach RUNNING (~20s)
   - Gets the public IP from the task's ENI
   - Waits for sshd to accept connections
   - Sets up the mngr host directory and certified data via SSH
2. `mngr connect` SSHs into the task's public IP
3. `mngr destroy` calls `ecs:StopTask`
4. `mngr list` discovers tasks by querying ECS for tasks tagged with `mngr-provider=fargate`

## Limitations

- **No snapshots** — Fargate tasks are ephemeral. Use EFS mounts for persistent data.
- **No stop/resume** — stopping destroys the task. Only create and destroy are supported.
- **Immutable tags** — ECS task tags cannot be modified after launch.
- **Startup time** — ~20s with a small image, longer with large images.
- **Cost** — billed per-second. Configure `default_idle_timeout` to auto-stop idle tasks.

## E2E Testing

```bash
python3 test_e2e.py
```

Launches a Fargate task, SSHs in, verifies git/tmux/file-io, and tears down. Requires `softmax-org` AWS profile with permission to assume into the cogtainer account.
