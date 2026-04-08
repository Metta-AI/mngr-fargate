#!/bin/bash
set -e

# Set up SSH authorized keys from environment variable
if [ -n "$MNGR_SSH_PUBLIC_KEY" ]; then
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    echo "$MNGR_SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi

# Add any extra authorized keys
if [ -n "$MNGR_EXTRA_AUTHORIZED_KEYS" ]; then
    mkdir -p /root/.ssh
    echo "$MNGR_EXTRA_AUTHORIZED_KEYS" >> /root/.ssh/authorized_keys
fi

# Create mngr host directory
if [ -n "$MNGR_HOST_DIR" ]; then
    mkdir -p "$MNGR_HOST_DIR"
fi

# Start sshd in the foreground
exec /usr/sbin/sshd -D -e
