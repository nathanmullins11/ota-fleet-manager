# Steps used to test Iteration 1 on Cloud VM

Create inbound rule for VM in its security group for TCP 1883 from my IP only. 

In WSL Local, start docker desktop and ssh into VM.
```
# First test connection to port 1883
nc -vz 3.17.39.28 1883
ssh -i "nathan.mullins.pem" ubuntu@ec2-3-17-39-28.us-east-2.compute.amazonaws.com
```
Replace with hostname of VM

Configure VM
```
git clone https://github.com/nathanmullins11/ota-fleet-manager.git
cd ota-fleet-manager
chmod +x bootstrap.sh
sudo ./boostrap.sh
exit
# re ssh from WSL local
docker run --rm hello-world
```

Build and Start Control Plane
```
cd ota-fleet-manager/deploy
cp .env.exmaple .env
docker compose up -d --build
docker compose ps
```

On WSL Local
```
cd ota-fleet-manager/deploy/device-agent
docker compose up -d --force-recreate
docker compose logs -f device-001
```

Back on Control Plane, show logs and registry entry
```
docker compose logs mosquitto | grep "as device-001"
docker compose logs mosquitto | grep "orchestrator 0 ota"
docker compose logs mosquitto | grep "PUBLISH" | grep -E "device-001|to orchestrator" | tail -4
docker compose logs orchestrator | grep "Heartbeat device-001" | tail -3
docker compose exec postgres psql -U ota -d ota -c "SELECT * FROM fleet_overview;"
curl localhost:8080/versions
```

Testing offline and back online device agent
```
# In WSL local
docker compose stop device-001
# wait 30-60 seconds
docker compose logs orchestrator | grep "marked offline"
# Back in WSL
docker compose start device-001

