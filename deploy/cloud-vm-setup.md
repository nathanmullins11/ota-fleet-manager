# Cloud VM Setup

Runs the control plane on an AWS EC2 instance, with simulated devices on a local computer.

## 1. Create the VM

- Launch an EC2 instance: Ubuntu 24.04, t3.small (2 CPU, 2 GB memory), General Purpose SSD
- Create or select a key pair
- Attach an Elastic IP, so the public IP stays the same when the instance stops and starts

## 2. Open the ports

Add these inbound rules to the security group. Find your IP with `curl -4 ifconfig.me`.

| Port | Source | Purpose |
|---|---|---|
| 22 | Your IP | SSH |
| 1883 | Your IP | Devices connect to the MQTT broker |
| 8080 | Your IP | Software registry: firmware downloads, and step 6 of the demo (`demo/`) |
| 8090 | Your IP | Visual demo helper (`demo/visual/`), only while presenting it; password protected |

Do not open port 1883 to `0.0.0.0/0`. The broker has no authentication until Iteration 3, so anyone who can reach it could publish messages. PostgreSQL only listens on the VM itself.

If your IP changes, update these rules.

## 3. Start the control plane

Log in:

```bash
ssh -i <key-file>.pem ubuntu@<VM_PUBLIC_IP>
```

Install Docker and clone the repository (set `REPO_BRANCH` to the branch you are testing):

```bash
curl -fsSLO https://raw.githubusercontent.com/nathanmullins11/ota-fleet-manager/main/deploy/bootstrap.sh
sudo REPO_URL=https://github.com/nathanmullins11/ota-fleet-manager.git REPO_BRANCH=main bash bootstrap.sh
```

Log out and back in so `ubuntu` can run Docker without `sudo`, then start the stack:

```bash
cd ~/ota-fleet-manager/deploy
cp .env.example .env      # set a strong POSTGRES_PASSWORD, and
                          # REGISTRY_PUBLIC_URL=http://<VM_PUBLIC_IP>:8080 so update
                          # commands point devices at the VM
docker compose up -d --build
docker compose ps
```

`mosquitto`, `postgres`, and `software-registry` should show `healthy`. `orchestrator` has no health label.

To get later changes:

```bash
cd ~/ota-fleet-manager
git pull
cd deploy
docker compose up -d --build
```

## 4. Connect a device from your local computer

Check that the broker is reachable. Some campus and work networks block port 1883:

```bash
nc -vz <VM_PUBLIC_IP> 1883
```

Then, in `deploy/device-agent/`, set `MQTT_HOST=<VM_PUBLIC_IP>` in `.env` and start the device:

```bash
docker compose up -d --force-recreate
docker compose logs -f device-001
```

Expected: `Connected to broker <VM_PUBLIC_IP>:1883 as device-001`.

## 5. Confirm on the VM

From `~/ota-fleet-manager/deploy`:

```bash
docker compose logs mosquitto | grep "as device-001"
docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM fleet_overview;"
```

Expected: the broker shows `device-001` connecting from your computer's public IP, and `fleet_overview` lists it as `healthy` with a recent heartbeat.

## Troubleshooting

| Device log | Likely cause |
|---|---|
| `Cannot reach broker ... timed out` | The security group rule is missing or has the wrong IP, or your network blocks port 1883 |
| `Cannot reach broker ... Connection refused` | The VM is reachable, but the broker is not running. Check `docker compose ps` on the VM |

## Cost

Stop the instance between tests. AWS charges for a public IPv4 address even while the instance is stopped, but only a few dollars a month.
