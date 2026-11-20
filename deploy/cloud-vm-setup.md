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
| 8883 | Your IP | Devices connect to the MQTT broker (TLS) |
| 8080 | Your IP | Software registry: firmware downloads, and step 6 of the demo (`demo/`) |
| 3000 | Your IP | Dashboard (Grafana): anyone who can reach it can view the fleet |

The broker accepts only TLS, signed-in clients, but keep 8883 limited to your IP anyway. Port 1883 is not used. PostgreSQL only listens on the VM itself.

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
cp .env.example .env      # set every "change-me" value to a strong secret, and
                          # REGISTRY_PUBLIC_URL=http://<VM_PUBLIC_IP>:8080 so update
                          # commands point devices at the VM
docker run --rm --user "$(id -u):$(id -g)" -v "$HOME/ota-fleet-manager:/repo" -w /repo \
    python:3.12-slim sh -c "pip install -q -r tools/requirements.txt && \
    python tools/mqtt_security.py setup --host <VM_PUBLIC_IP>"
docker compose up -d --build
docker compose ps
```

`mosquitto`, `postgres`, and `software-registry` should show `healthy`. `orchestrator` has no health label.

The setup step writes the broker's TLS certificate (valid for the VM's IP) and password file into `deploy/mqtt/generated/`. `demo/cloud/control-plane.sh` does all of this for you.

To get later changes:

```bash
cd ~/ota-fleet-manager
git pull
cd deploy
docker compose up -d --build
```

## 4. Connect a device from your local computer

Check that the broker is reachable. Some campus and work networks block unusual ports:

```bash
nc -vz <VM_PUBLIC_IP> 8883
```

Copy the CA certificate and device secret from the VM (once, and again only if they change), from the repository root:

```bash
scp -i <key-file>.pem ubuntu@<VM_PUBLIC_IP>:ota-fleet-manager/deploy/mqtt/generated/{ca.crt,device.env} deploy/mqtt/generated/
```

Then, in `deploy/device-agent/`, set `MQTT_HOST=<VM_PUBLIC_IP>` in `.env`. Either start a fleet with `./fleet.sh up 10` (it reads `device.env`), or put device-001's password in `.env` as `DEVICE_001_PASSWORD` (`python tools/mqtt_security.py device-password device-001 --env-file deploy/mqtt/generated/device.env`) and start the single Compose device:

```bash
docker compose up -d --force-recreate
docker compose logs -f device-001
```

Expected: `Connected to broker <VM_PUBLIC_IP>:8883 as device-001 (TLS)`.

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
| `Cannot reach broker ... timed out` | The security group rule is missing or has the wrong IP, or your network blocks port 8883 |
| `certificate verify failed` | `ca.crt` is from an older setup, or the VM's IP is not in the broker certificate: rerun setup with `--host <VM_PUBLIC_IP>` on the VM and copy `ca.crt` again |
| `Broker refused the connection: Not authorized` | The device's password does not match: copy `device.env` again |
| `Cannot reach broker ... Connection refused` | The VM is reachable, but the broker is not running. Check `docker compose ps` on the VM |

## Cost

Stop the instance between tests. AWS charges for a public IPv4 address even while the instance is stopped, but only a few dollars a month.
