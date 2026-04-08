FROM debian:bookworm-slim

# System packages required by mngr hosts
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    git \
    tmux \
    rsync \
    jq \
    curl \
    procps \
    sudo \
    ca-certificates \
    ncurses-term \
    && rm -rf /var/lib/apt/lists/*

# Configure sshd
RUN mkdir -p /run/sshd && \
    ssh-keygen -A && \
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config && \
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config

# Entrypoint script: reads MNGR_SSH_PUBLIC_KEY env var and starts sshd
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22

ENTRYPOINT ["/entrypoint.sh"]
