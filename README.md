# WireGuard VPN on AWS Lightsail (Terraform)

[![CI](https://github.com/Luqos/wireguard-lightsail-terraform/actions/workflows/ci.yml/badge.svg)](https://github.com/Luqos/wireguard-lightsail-terraform/actions/workflows/ci.yml)

One `terraform apply` creates a Lightsail instance in Asia Pacific (Malaysia), a static IP, and a locked-down firewall. On first boot the instance installs WireGuard on UDP 443, with an egress allowlist that only forwards Discord and VALORANT ports.

```
.
├── versions.tf               # Terraform + AWS provider
├── variables.tf              # region, name, bundle, SSH CIDRs
├── main.tf                   # instance, static IP, firewall
├── outputs.tf                # endpoint IP + next steps
├── terraform.tfvars.example
├── scripts/
│   ├── user_data.sh.tftpl    # first-boot launcher (POSIX, rendered by Terraform)
│   └── wg-setup.sh           # server install, peers, egress filter
├── ci/
│   ├── check_launcher.py     # renders the launcher, rejects bash-only syntax
│   └── backend_s3.tf.txt     # S3 backend, copied in by the Deploy workflow only
├── iam/
│   ├── github-oidc-trust-policy.json
│   └── terraform-deploy-permissions.json
└── .github/workflows/
    ├── ci.yml                # lint + validate on every push, no AWS access
    └── deploy.yml            # manual plan / apply / destroy via OIDC
```

## Prerequisites

- Terraform 1.6+ and AWS CLI v2 installed.
- Credentials for the **project account** that owns the Lightsail instance (not the management account):
  ```
  aws configure sso          # pick the project account, name the profile e.g. wg
  export AWS_PROFILE=wg      # PowerShell: $env:AWS_PROFILE="wg"
  ```
- The target Region allowed by the organization's SCP (`RegionFloor`) and enabled as an opt-in Region.

## Check the IDs once

Lightsail bundle and blueprint IDs differ by Region and change over time:

```
aws lightsail get-bundles --region ap-southeast-5 --query "bundles[?price==\`5.0\`].[bundleId,price,ramSizeInGb,transferPerMonthInGb]" --output table
aws lightsail get-blueprints --region ap-southeast-5 --query "blueprints[?contains(blueprintId,'ubuntu')].blueprintId" --output table
```

Put the right values in `terraform.tfvars` if they differ from the defaults (`nano_3_1`, `ubuntu_24_04`). In Malaysia the USD 5 IPv4 plan is `nano_3_1`: 512 MB RAM and 512 GB transfer a month (`nano_3_0` does not exist there). At that allowance the box serves about 5–8 full-tunnel users.

## Deploy

```
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

**Several servers.** Each server gets its own workspace (separate state) and var file in `envs/`:

```
terraform workspace select my     # or: terraform workspace new my
terraform apply -var-file=envs/my.tfvars
```

`envs/my.tfvars` is the Malaysia server. To add another server, copy it to a new file and use a new workspace. Keep private values such as `ssh_cidrs` (your home IP) and `alert_email` in the git-ignored `terraform.tfvars`; it applies to every workspace.

### Transfer alert

`alerts.tf` creates a Lightsail alarm (`<name>-transfer-pace`) through the `awscc` provider, because the `aws` provider has no Lightsail alarm resource. It emails you if outbound traffic is above about 30 MiB per 5 minutes for 6 of the last 24 hours. At that pace, 512 GB is gone in a month.

The email contact method is a one-time CLI step. Cloud Control has no `AWS::Lightsail::ContactMethod` type in ap-southeast-5.

```
aws lightsail create-contact-method --region ap-southeast-5 --protocol Email --contact-endpoint you@example.com
```

Click the verification link AWS sends, then set `alert_email` in `terraform.tfvars` and apply.

Then in the Lightsail console, open the instance, click **Connect using SSH**, and run:

```
sudo cloud-init status --wait     # wait for the first-boot install
sudo wg-setup add <name>          # one peer per person
```

## Day-to-day commands

| Command | Does |
| --- | --- |
| `sudo wg-setup add <name>` | New peer, prints QR code, saves `/root/wg-clients/<name>.conf` |
| `sudo wg-setup remove <name>` | Revokes one peer, no downtime for others |
| `sudo wg-setup list` | Peers and last handshake |
| `sudo wg-setup filter status` | Shows the active egress rules |
| `sudo wg-setup filter off` | Plain full tunnel (all ports) |
| `sudo wg-setup filter on` | Back to the Discord/VALORANT allowlist |
| `sudo wg-setup stats` | Per-peer usage as TSV, with no keys (used by the monitor app) |
| `sudo wg-setup monitor '<pubkey>'` | Creates the read-only `wgmon` account for the monitor app |
| `sudo wg-setup admin '<pubkey>'` | Creates the add/remove-only `wgadmin` account for the monitor app |
| `sudo wg-setup ssh-from <ip>...` | Only these tunnel IPs may SSH in over WireGuard (`any` = every peer, no args = show) |

## Monitor app

`monitor/wgmon.py` is a local web app for tracking users and data transfer. It needs only Python 3.12+, the AWS CLI and OpenSSH, with no pip packages.

```
$env:AWS_PROFILE = "wg"
py -3.12 monitor/wgmon.py          # opens http://127.0.0.1:8765
```

- **Users:** who is online, active (7 days) or idle, and this month's usage per person, with paid ticks and paid-until dates. New users start as a **trial**, and ticking Paid makes them a **member**.
- **Add user** creates the peer on the server and shows the QR code and `.conf` once. **Remove** revokes access on the server, then deletes the record, after you type the name to confirm.
- It flags peers that exist on a server but aren't tracked, and tracked peers that are missing from the server.
- **Servers:** monthly transfer against the plan allowance (Lightsail counts in + out), a month-end projection, daily bars and the alarm state.
- **Setup:** the one-line command that connects a server to the app.

How it reaches the servers safely:

- `wg show wg0 dump` prints the server private key and preshared keys, so the app never runs it. `wg-setup stats` joins the key-free views instead.
- The `wgmon` account's SSH key is pinned to that one command (`restrict,command=`). sudo allows only `wg-setup stats`, and root owns its `authorized_keys`. A leaked key reveals only usage numbers.
- Adding and removing users uses a **separate** `wgadmin` key. Its forced command is a gate script that accepts only `add <name>` or `remove <name>`, with names matching `[a-z0-9][a-z0-9._-]{0,31}`; anything else is refused and logged. sudo allows only `wg-setup api-add` and `api-remove`.
  - The server allows at most 10 adds per 24 hours and logs every call to syslog and `/var/log/wg-admin.log`.
  - A leaked admin key can add or remove peers. It can't open a shell or read anyone's existing config.
- New configs contain the user's private key. The app shows them once, as an `<img>` QR code (an SVG in an image can't run scripts) plus a download, and never stores them.
- `wg-setup` itself also validates peer names now, so a name like `../x` can't reach a file path.
- The app SSHes to `10.8.0.1`, the server's own address inside the WireGuard tunnel, so it works from any home IP as long as WireGuard is connected on your PC. Port 22 on the public IP can stay closed to everything except the Lightsail browser console (`ssh_cidrs = []`). If the tunnel isn't up, the app falls back to the public IP and then tells you to connect WireGuard.
  - Host keys are pinned under each server's **public** IP, even over the tunnel (`HostKeyAlias`). Every server is `10.8.0.1` inside its own tunnel, so if your PC is connected to a different server, the key check fails and nothing runs.
  - A new server is pinned over the tunnel on first contact, unless the key it offers is already pinned for another server. Compare it with the fingerprint `wg-setup monitor` prints.
  - `sudo wg-setup ssh-from 10.8.0.2` limits SSH over the tunnel to your own peer, so other users' devices can't even reach sshd. `ssh-from any` undoes it.
- The app listens on 127.0.0.1 only, rejects foreign `Host`/`Origin` headers, and needs a per-launch token for every API call. It sends a strict CSP. AWS calls are limited to read-only Lightsail operations.
- The SQLite database (contacts, payments) and the SSH key live in `%LOCALAPPDATA%\wg-monitor`, outside the repo.

Per-peer counters reset when a server reboots. The app adds up the changes between refreshes, every 15 minutes while it runs, so traffic between the last refresh and a reboot is lost.

## Egress allowlist

Edit `/etc/wireguard/egress.conf` on the server, then run `sudo wg-setup filter on` to apply.

| Protocol | Ports | For |
| --- | --- | --- |
| TCP | 53, 80, 443 | DNS, HTTP, HTTPS (Riot login, Discord API) |
| TCP | 2099, 5222, 5223, 8088, 8393–8400 | VALORANT / Riot client |
| UDP | 53, 3478 | DNS, STUN |
| UDP | 7000–8000, 8088, 8180–8181 | VALORANT game client |
| UDP | 50000–65535 | Discord voice; VALORANT AP/SEA voice (54000–54012) |

VALORANT ports are from Riot Support's "How to Set Up Port Forwarding" page. Discord does not publish an official list; 50000–65535 is the widely used voice range.

Peers also cannot reach each other or the instance metadata service.

**Limits of a port filter:** TCP 443 must stay open for Riot and Discord, so web browsing still works through the tunnel. The filter blocks torrents, mail, and other apps, but does not cut bandwidth much. Other apps on non-allowed ports fail while the tunnel is on, so users should switch it on only when gaming.

## Things that bite

- **Lightsail runs `user_data` under `/bin/sh`, not bash.** It merges your script into its own launch script, so a `#!/bin/bash` shebang is ignored. `set -o pipefail` made the first boot fail. The launcher therefore stays POSIX and hands off to `bash /usr/local/sbin/wg-setup install`; CI renders it and parses it with `dash -n` to keep it that way.
- **Changing `wg-setup.sh` does not update a running server.** `user_data` is ignored after creation so peers are never wiped by accident. To rebuild on purpose: `terraform apply -replace=aws_lightsail_instance.wg`. A rebuild generates a new server key, so every peer needs a new config.
- **Name clash.** If a manually created instance already uses the same name, change `name` in `terraform.tfvars`.
- **Destroy cleans up the static IP too.** `terraform destroy` removes the instance, static IP and firewall, so no orphaned static IP keeps billing.
- **State holds no secrets.** Keys are generated on the server and never pass through Terraform.

## CI

`.github/workflows/ci.yml` runs on every push and pull request, with no AWS credentials:

- ShellCheck on `wg-setup.sh`
- Unit tests for the monitor app
- Renders `user_data.sh.tftpl`, rejects `pipefail` and `[[` in the launcher, then parses the result with `dash -n`
- `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`

## CD (optional, dormant until set up)

`.github/workflows/deploy.yml` is started by hand (**Actions → Deploy → Run workflow**) with `plan`, `apply` or `destroy`. It signs in to AWS with GitHub OIDC (no stored keys), keeps state in S3 with native locking (`use_lockfile`), and posts the plan to the run summary. `apply` and `destroy` wait for approval on the `production` environment, then run the exact saved plan.

The S3 backend is only added inside the workflow (`ci/backend_s3.tf.txt`), so local runs keep using local state. **Use one or the other for a given deployment**: CI state and local state don't know about each other.

### One-time setup (project account, profile `wg`)

1. State bucket (versioned; new buckets block public access by default):
   ```
   aws s3api create-bucket --bucket STATE_BUCKET --region ap-southeast-5 --create-bucket-configuration LocationConstraint=ap-southeast-5
   aws s3api put-bucket-versioning --bucket STATE_BUCKET --versioning-configuration Status=Enabled
   ```
2. GitHub OIDC provider (once per account):
   ```
   aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com --client-id-list sts.amazonaws.com
   ```
3. Replace `ACCOUNT_ID`, `GITHUB_USER`, `REPO_NAME` and `STATE_BUCKET` in `iam/*.json`, then create the role:
   ```
   aws iam create-role --role-name gha-wireguard-terraform --assume-role-policy-document file://iam/github-oidc-trust-policy.json
   aws iam put-role-policy --role-name gha-wireguard-terraform --policy-name terraform-deploy --policy-document file://iam/terraform-deploy-permissions.json
   ```
   The trust policy only accepts this repo's `main` branch and its `production` environment. Lightsail access is limited to `ap-southeast-5`; S3 access to the one state key and its lock file.
4. In the GitHub repo:
   - **Settings → Environments → New environment** `production`, add yourself as a required reviewer.
   - **Settings → Secrets and variables → Actions → Variables**: `AWS_ROLE_ARN` (the role's ARN) and `TF_STATE_BUCKET`. Optional: `TF_STATE_REGION` (defaults to `ap-southeast-5`).
   - Also set `ALERT_EMAIL` and `SSH_CIDRS` (for example `["203.0.113.7/32"]`). These replace `terraform.tfvars` in CI. If they're unset, a deploy deletes the alarm and closes SSH to your IP.

The workflow uses the `my` workspace and `envs/my.tfvars`, so state lives at `env:/my/wireguard-vpn/terraform.tfstate` in the bucket.
