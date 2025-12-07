# Recovery Manager V2 - Deployment Guide

## Changes Made

### 1. Fixed PyMonNet API Integration
- **Issue**: Code expected `/api/metrics` endpoint with array format
- **Fix**: Changed to `/nodes` endpoint and parse flat dictionary format
- **Enhancement**: Auto-fetch containers from Docker API when node is stressed but PyMonNet hasn't sent container metrics yet

### 2. Intelligent Container Detection
The recovery manager now works even when PyMonNet container data is delayed:
- Detects when node CPU/MEM exceeds thresholds
- If no container data available, queries Docker API directly on that node
- Identifies which service containers are running on the stressed node

## Deployment Steps

### Step 1: Build and Push Image

```bash
cd /path/to/fyp
git pull
sudo docker build -t docker-registry.amirmuz.com/swarm-recovery-manager:22 .
sudo docker push docker-registry.amirmuz.com/swarm-recovery-manager:22
```

### Step 2: Remove Old Service

```bash
sudo docker service rm swarm-recovery-manager
```

### Step 3: Deploy New Service

```bash
sudo docker service create \
  --name swarm-recovery-manager \
  --constraint 'node.role == manager' \
  --replicas 1 \
  --network pymonnet-net \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  --env PYMONNET_URL=http://pymonnet-server:6969 \
  --env LOG_LEVEL=INFO \
  --env REQUEST_TIMEOUT=3 \
  --env POLL_INTERVAL=1 \
  --env NODE_CPU_CRITICAL=50 \
  --env NODE_MEM_CRITICAL=60 \
  --env CONTAINER_CPU_THRESHOLD=70 \
  --env NETWORK_OUT_THRESHOLD=40 \
  --env COOLDOWN_MIGRATE=15 \
  --env COOLDOWN_SCALE_UP=10 \
  --env COOLDOWN_SCALE_DOWN=10 \
  --env SCALE_DOWN_FACTOR=0.8 \
  --env REMOTE_DOCKER_HOSTS=worker-1=tcp://192.168.2.51:2375,worker-2=tcp://192.168.2.52:2375,worker-3=tcp://192.168.2.53:2375,worker-4=tcp://192.168.2.54:2375 \
  --env REMOTE_DOCKER_TLS_VERIFY=false \
  docker-registry.amirmuz.com/swarm-recovery-manager:22
```

### Step 4: Monitor Logs

```bash
docker service logs -f swarm-recovery-manager
```

## Expected Log Output

### On Startup
```json
{"timestamp": "...", "level": "INFO", "message": "Local Docker client initialized successfully"}
{"timestamp": "...", "level": "INFO", "message": "Remote Docker client ready for node worker-1"}
{"timestamp": "...", "level": "INFO", "message": "Recovery Manager started"}
{"timestamp": "...", "level": "INFO", "message": "Fetched metrics for 5 nodes"}
```

### When Detecting Issues
```json
{"timestamp": "...", "level": "INFO", "message": "Node worker-1 stressed but no container data - fetching from Docker API"}
{"timestamp": "...", "level": "INFO", "message": "Rules triggered: 1 actions pending", "action_count": 1}
{"timestamp": "...", "level": "INFO", "message": "Executing action: migrate_container for organic-web-stress"}
```

### Scenario 1 (Migration)
```json
{"timestamp": "...", "level": "INFO", "message": "Starting migration for organic-web-stress from worker-1"}
{"timestamp": "...", "level": "INFO", "message": "Step 1: Adding constraint to EXCLUDE worker-1"}
{"timestamp": "...", "level": "INFO", "message": "Step 2: Scaling up from 1 to 2"}
{"timestamp": "...", "level": "INFO", "message": "Step 3: Waiting for new container to start (20s)"}
{"timestamp": "...", "level": "INFO", "message": "Step 4: Scaling back down to 1"}
{"timestamp": "...", "level": "INFO", "message": "Step 5: Removing placement constraint"}
{"timestamp": "...", "level": "INFO", "message": "Migration completed for organic-web-stress"}
```

### Scenario 2 (Scale Up)
```json
{"timestamp": "...", "level": "INFO", "message": "Executing action: scale_up for organic-web-stress"}
{"timestamp": "...", "level": "INFO", "message": "Scaled up organic-web-stress from 1 to 2"}
```

### Scenario 2 (Scale Down)
```json
{"timestamp": "...", "level": "INFO", "message": "Executing action: scale_down for organic-web-stress"}
{"timestamp": "...", "level": "INFO", "message": "Scaled down organic-web-stress from 3 to 2"}
```

## How It Works

### Scenario 1: Resource Exhaustion (Migration)
**Triggers when**: CPU/MEM high, Network LOW
**Action**: Migrate container to healthy node
**Process**:
1. Adds placement constraint excluding problematic node
2. Scales up by 1 (creates new container on different node)
3. Waits for new container to be healthy
4. Scales back down (removes old container)
5. Removes constraint

**Result**: Zero downtime migration!

### Scenario 2: High Traffic (Scaling)
**Scale Up triggers when**: CPU/MEM/Network ALL high
**Scale Down triggers when**: Total usage < threshold × (replicas - 1) × 0.8

**Actions**:
- Adds 1 replica at a time when traffic high
- Removes 1 replica at a time when idle
- Never goes below 1 replica

## Configuration

### Thresholds
- `NODE_CPU_CRITICAL=50` - Node CPU % to trigger action
- `NODE_MEM_CRITICAL=60` - Node Memory % to trigger action
- `CONTAINER_CPU_THRESHOLD=70` - Container CPU % threshold
- `NETWORK_OUT_THRESHOLD=40` - Network Mbps threshold for high traffic detection

### Cooldowns (prevents rapid repeated actions)
- `COOLDOWN_MIGRATE=15` - Seconds between migrations
- `COOLDOWN_SCALE_UP=10` - Seconds between scale ups
- `COOLDOWN_SCALE_DOWN=10` - Seconds between scale downs

### Scale Down Sensitivity
- `SCALE_DOWN_FACTOR=0.8` - Scale down when usage < 80% of threshold

## Troubleshooting

### No actions being taken
1. Check if metrics are being fetched: Look for `"Fetched metrics for X nodes"` in logs
2. Check if nodes are stressed: Node CPU/MEM must exceed thresholds
3. Check cooldowns: Actions might be in cooldown period
4. Check if service exists: Use `docker service ls` to verify service names

### Migration not working
1. Verify remote Docker hosts are accessible
2. Check placement constraints: `docker service inspect organic-web-stress`
3. Ensure service has resources to run on other nodes

### Scale operations failing
1. Check service mode: Must be replicated mode
2. Verify Docker swarm has capacity for more replicas
3. Check for resource constraints in service spec

## Optional: Lower PyMonNet Thresholds

If you want PyMonNet to send container metrics earlier (matching recovery manager thresholds), update PyMonNet agent environment:

```bash
--env CPU_THRESHOLD=50 \
--env MEM_THRESHOLD=60 \
```

This makes PyMonNet more proactive, but the recovery manager now works even without this change!
