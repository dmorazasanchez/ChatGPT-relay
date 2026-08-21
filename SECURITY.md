# Security

ChatGPT Relay is intentionally powerful. Treat the queue repository as a remote-control credential for the Unix account running the daemon.

## Trust boundary

For GitHub transport, **write access to the queue repository is authorization to execute jobs**. The relay does not put a reusable secret in job JSON because that secret would remain in Git history after the job file is deleted.

Use a dedicated private queue repository. Do not use a shared source repository as the queue.

## Shell jobs are code execution

`op: shell` runs `/bin/bash -lc <command>` as the relay's Unix user. `allowed_roots` validates the shell job's working directory, but it does not sandbox the shell itself. A command can access other files, the network, credentials and devices available to that Unix account.

For stronger isolation, run the relay as a dedicated Unix user, inside a VM/container, or with an OS sandbox appropriate to your threat model.

## Sudo

If the relay user has passwordless sudo, a shell job may effectively have root access. Avoid that unless it is an intentional part of your setup.

## Queue contents

Commands and results are committed to the queue repository. Git deletion does not erase Git history. Never place passwords, API keys, private keys or other long-lived secrets directly in job JSON or command strings.

Prefer environment/configuration already present on the host when a tool needs credentials.

## Local HTTP API

The optional HTTP API is bound to `127.0.0.1` by default and uses a separate token stored in `~/.config/chatgpt-relay/config.json` with mode 0600.

Do not expose the HTTP port publicly without adding a secure authenticated transport in front of it.

## Results can be sensitive

stdout/stderr may contain source code, paths, environment details or other sensitive information. The queue repository should therefore be private even though job authentication is based on write access rather than secrecy.

## Recovery semantics

The relay uses conservative at-most-once recovery. If it restarts while a job is marked `running`, it does not automatically rerun the command. This avoids blindly repeating destructive side effects, but it cannot guarantee exactly-once execution for arbitrary commands.

## Reporting vulnerabilities

Please open a GitHub issue for non-sensitive security problems. For a vulnerability that would expose user systems or credentials, contact the repository owner privately before publishing exploit details.
