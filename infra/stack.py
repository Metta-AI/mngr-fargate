"""CDK stack for mngr-fargate infrastructure.

Provisions everything needed to run mngr agents on ECS Fargate:
- VPC with public subnets
- ECS Fargate cluster
- Task definition with sshd-enabled container
- ECR repository for the agent image
- Security group allowing SSH (port 22)
- IAM roles for task execution and runtime permissions
- CloudWatch log group

Usage:
    cdk deploy --context account=123456789012 --context region=us-east-1
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct


class MngrFargateStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- VPC ----
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,  # Public subnets only — tasks get public IPs
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )

        # ---- ECR repository ----
        repo = ecr.Repository(
            self,
            "Repo",
            repository_name="mngr-fargate",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # ---- ECS cluster ----
        cluster = ecs.Cluster(self, "Cluster", cluster_name="mngr", vpc=vpc)

        # ---- IAM: task execution role (pulls images, writes logs) ----
        execution_role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        # ---- IAM: task role (what the running container can do) ----
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        # SSM for ECS Exec (fallback shell access)
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                ],
                resources=["*"],
            )
        )
        # Bedrock for Claude Code
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/anthropic.*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/us.anthropic.*",
                ],
            )
        )
        # Secrets Manager for per-agent secrets
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:mngr/*",
                ],
            )
        )

        # ---- Log group ----
        log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name="/mngr/fargate",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- Task definition ----
        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            family="mngr-task",
            cpu=1024,
            memory_limit_mib=4096,
            execution_role=execution_role,
            task_role=task_role,
        )

        task_def.add_container(
            "agent",
            image=ecs.ContainerImage.from_ecr_repository(repo),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="mngr",
                log_group=log_group,
            ),
            environment={
                "MNGR_HOST_DIR": "/mngr",
            },
            port_mappings=[
                ecs.PortMapping(container_port=22, protocol=ecs.Protocol.TCP),
            ],
        )

        # ---- Security group ----
        task_sg = ec2.SecurityGroup(
            self,
            "TaskSg",
            vpc=vpc,
            description="mngr Fargate tasks — SSH access",
            allow_all_outbound=True,
        )
        task_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(22),
            "SSH access for mngr",
        )

        # ---- IAM: caller permissions (for the machine running `mngr`) ----
        caller_policy = iam.ManagedPolicy(
            self,
            "CallerPolicy",
            managed_policy_name="mngr-fargate-caller",
            statements=[
                iam.PolicyStatement(
                    actions=[
                        "ecs:RunTask",
                        "ecs:StopTask",
                        "ecs:DescribeTasks",
                        "ecs:ListTasks",
                        "ecs:TagResource",
                    ],
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    actions=["iam:PassRole"],
                    resources=[execution_role.role_arn, task_role.role_arn],
                ),
                iam.PolicyStatement(
                    actions=[
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DescribeSubnets",
                    ],
                    resources=["*"],
                ),
            ],
        )

        # ---- Outputs (paste into ~/.mngr/config.toml) ----
        public_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PUBLIC)

        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(self, "TaskDefinition", value=task_def.family or "mngr-task")
        CfnOutput(self, "EcrRepo", value=repo.repository_uri)
        CfnOutput(
            self,
            "Subnets",
            value=",".join(s.subnet_id for s in public_subnets.subnets),
        )
        CfnOutput(self, "SecurityGroup", value=task_sg.security_group_id)
        CfnOutput(self, "CallerPolicyArn", value=caller_policy.managed_policy_arn)
        CfnOutput(self, "TaskRoleArn", value=task_role.role_arn)
        CfnOutput(self, "ExecutionRoleArn", value=execution_role.role_arn)
