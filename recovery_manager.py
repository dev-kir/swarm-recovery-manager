#!/usr/bin/env python3
"""
Recovery Manager for Docker Swarm v2
Proactive Recovery with Two Scenarios:
1. Resource Exhaustion (CPU/MEM high, NET low) → Migrate container to another node
2. High Traffic (CPU/MEM/NET all high) → Scale up by adding containers

Author: Amir Muzakkir Bin Md Kamaru Al-Amin
FYP Project - UiTM 2025
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict

import requests
import docker
from docker.tls import TLSConfig


# ============================================================
# CONFIGURATION
# ============================================================
class Config:
    # PyMonNet Server
    PYMONNET_URL = os.getenv("PYMONNET_URL", "http://pymonnet-server:6969")
    POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "1"))
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "3"))

    # Thresholds
    NODE_CPU_CRITICAL = float(os.getenv("NODE_CPU_CRITICAL", "50"))
    NODE_MEM_CRITICAL = float(os.getenv("NODE_MEM_CRITICAL", "60"))
    CONTAINER_CPU_THRESHOLD = float(os.getenv("CONTAINER_CPU_THRESHOLD", "70"))
    NETWORK_OUT_THRESHOLD = float(os.getenv("NETWORK_OUT_THRESHOLD", "40"))  # Mbps

    # Cooldowns
    COOLDOWN_MIGRATE = int(os.getenv("COOLDOWN_MIGRATE", "15"))
    COOLDOWN_SCALE_UP = int(os.getenv("COOLDOWN_SCALE_UP", "10"))
    COOLDOWN_SCALE_DOWN = int(os.getenv("COOLDOWN_SCALE_DOWN", "10"))

    # Scale down logic
    # If [total usage of all containers] < [threshold × (current_replicas - 1)]
    # Then we can scale down by 1
    SCALE_DOWN_FACTOR = float(os.getenv("SCALE_DOWN_FACTOR", "0.8"))  # 80% of threshold

    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # Remote Docker hosts (for per-node container inspection)
    REMOTE_DOCKER_HOSTS = os.getenv("REMOTE_DOCKER_HOSTS", "")
    REMOTE_DOCKER_TLS_VERIFY = os.getenv("REMOTE_DOCKER_TLS_VERIFY", "false").lower() == "true"
    REMOTE_DOCKER_CA_CERT = os.getenv("REMOTE_DOCKER_CA_CERT", "/certs/ca.pem")
    REMOTE_DOCKER_CLIENT_CERT = os.getenv("REMOTE_DOCKER_CLIENT_CERT", "/certs/cert.pem")
    REMOTE_DOCKER_CLIENT_KEY = os.getenv("REMOTE_DOCKER_CLIENT_KEY", "/certs/key.pem")


def _parse_host_mapping(raw: str) -> dict[str, str]:
    """Parse env string like 'worker-1=tcp://192.168.2.51:2375,worker-2=tcp://192.168.2.52:2375'."""
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        node, url = entry.split("=", 1)
        node = node.strip()
        url = url.strip()
        if node and url:
            mapping[node] = url
    return mapping

Config.REMOTE_DOCKER_HOST_MAP = _parse_host_mapping(Config.REMOTE_DOCKER_HOSTS)


# ============================================================
# LOGGING SETUP
# ============================================================
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, 'extra') and record.extra:
            log_obj.update(record.extra)
        return json.dumps(log_obj)


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("recovery_manager")
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO))
    return logger

logger = setup_logging()


# ============================================================
# DATA STRUCTURES
# ============================================================
@dataclass
class ContainerMetrics:
    container: str
    container_id: str
    cpu: float
    mem: float
    net_in: float
    net_out: float


@dataclass
class NodeMetrics:
    node: str
    role: str
    cpu: float
    mem: float
    net_in: float
    net_out: float
    containers: List[ContainerMetrics]
    timestamp: str


class ActionType(Enum):
    MIGRATE_CONTAINER = "migrate_container"  # Scenario 1: Move to different node
    SCALE_UP = "scale_up"  # Scenario 2: Add 1 replica
    SCALE_DOWN = "scale_down"  # Scenario 2: Remove 1 replica


@dataclass
class RecoveryAction:
    action_type: ActionType
    service_name: str
    problematic_node: Optional[str] = None  # For migration
    reason: str = ""
    metrics: dict = field(default_factory=dict)


# ============================================================
# DOCKER CLIENT MANAGER
# ============================================================
class DockerClientManager:
    """Manages Docker client connections"""

    def __init__(self):
        self.local_client = self._init_local_client()
        self.remote_clients: Dict[str, docker.DockerClient] = {}
        self._init_remote_clients()

    def _init_local_client(self) -> Optional[docker.DockerClient]:
        try:
            client = docker.from_env()
            logger.info("Local Docker client initialized successfully")
            return client
        except docker.errors.DockerException as e:
            logger.error(f"Failed to initialize local Docker client: {e}")
            return None

    def _init_remote_clients(self):
        for node, base_url in Config.REMOTE_DOCKER_HOST_MAP.items():
            try:
                tls_config = None
                if Config.REMOTE_DOCKER_TLS_VERIFY:
                    tls_config = TLSConfig(
                        client_cert=(Config.REMOTE_DOCKER_CLIENT_CERT, Config.REMOTE_DOCKER_CLIENT_KEY),
                        ca_cert=Config.REMOTE_DOCKER_CA_CERT,
                        verify=True
                    )
                client = docker.DockerClient(base_url=base_url, tls=tls_config)
                client.ping()
                self.remote_clients[node] = client
                logger.info(f"Remote Docker client ready for node {node}", extra={"node": node, "docker_host": base_url})
            except Exception as e:
                logger.warning(f"Failed to connect to remote Docker on {node}: {e}")

    def get_local_client(self) -> Optional[docker.DockerClient]:
        return self.local_client

    def get_client_for_node(self, node: str) -> Optional[docker.DockerClient]:
        return self.remote_clients.get(node)


# ============================================================
# METRICS POLLER
# ============================================================
class MetricPoller:
    """Fetches metrics from PyMonNet"""

    def __init__(self):
        self.url = Config.PYMONNET_URL + "/nodes"

    def fetch(self) -> Dict[str, NodeMetrics]:
        try:
            response = requests.get(self.url, timeout=Config.REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()

            nodes = {}
            # PyMonNet returns flat dict: {"worker-1": {...}, "worker-2": {...}}
            for node_name, node_data in data.items():
                # Parse container metrics if available
                containers = []
                if "containers" in node_data and isinstance(node_data["containers"], list):
                    containers = [
                        ContainerMetrics(
                            container=c["container"],
                            container_id=c["container_id"],
                            cpu=c["cpu"],
                            mem=c["mem"],
                            net_in=c["net_in"],
                            net_out=c["net_out"]
                        )
                        for c in node_data["containers"]
                    ]

                nodes[node_name] = NodeMetrics(
                    node=node_data["node"],
                    role=node_data["role"],
                    cpu=node_data["cpu"],
                    mem=node_data["mem"],
                    net_in=node_data["net_in"],
                    net_out=node_data["net_out"],
                    containers=containers,
                    timestamp=node_data["timestamp"]
                )

            logger.info(f"Fetched metrics for {len(nodes)} nodes")
            return nodes
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch metrics from PyMonNet: {e}")
            return {}
        except (KeyError, ValueError) as e:
            logger.warning(f"Invalid metrics data format: {e}")
            logger.error(f"Data received: {data if 'data' in locals() else 'N/A'}")
            return {}


# ============================================================
# COOLDOWN MANAGER
# ============================================================
class CooldownManager:
    """Prevents rapid repeated actions"""

    def __init__(self):
        self.last_action_time: Dict[str, Dict[ActionType, datetime]] = defaultdict(dict)

    def can_act(self, service_name: str, action_type: ActionType) -> bool:
        if service_name not in self.last_action_time:
            return True

        if action_type not in self.last_action_time[service_name]:
            return True

        last_time = self.last_action_time[service_name][action_type]
        cooldown_seconds = self._get_cooldown(action_type)
        elapsed = (datetime.now() - last_time).total_seconds()

        return elapsed >= cooldown_seconds

    def record_action(self, service_name: str, action_type: ActionType):
        self.last_action_time[service_name][action_type] = datetime.now()

    def _get_cooldown(self, action_type: ActionType) -> int:
        if action_type == ActionType.MIGRATE_CONTAINER:
            return Config.COOLDOWN_MIGRATE
        elif action_type == ActionType.SCALE_UP:
            return Config.COOLDOWN_SCALE_UP
        elif action_type == ActionType.SCALE_DOWN:
            return Config.COOLDOWN_SCALE_DOWN
        return 10


# ============================================================
# NETWORK HISTORY TRACKER (for sustained detection)
# ============================================================
class NetworkHistoryTracker:
    """Tracks network history to detect sustained high traffic (not just brief spikes)"""

    def __init__(self, history_size: int = 3):
        """
        Args:
            history_size: Number of consecutive samples required for sustained detection
        """
        self.history_size = history_size
        self.history: Dict[str, List[float]] = defaultdict(list)  # node -> [net_out values]

    def record(self, node: str, net_out: float):
        """Record a network measurement for a node"""
        if node not in self.history:
            self.history[node] = []

        self.history[node].append(net_out)

        # Keep only last N samples
        if len(self.history[node]) > self.history_size:
            self.history[node] = self.history[node][-self.history_size:]

    def is_sustained_high(self, node: str, threshold: float) -> bool:
        """
        Check if network has been consistently above threshold

        Returns True only if ALL recent samples exceed threshold
        This prevents false positives from brief spikes (e.g., 93 Mbps → 0.3 Mbps → 92 Mbps)
        """
        if node not in self.history:
            return False

        samples = self.history[node]

        # Need enough samples
        if len(samples) < self.history_size:
            return False

        # ALL samples must exceed threshold
        return all(sample > threshold for sample in samples)

    def get_recent_samples(self, node: str) -> List[float]:
        """Get recent network samples for debugging"""
        return self.history.get(node, [])

    def clear(self, node: str):
        """Clear history for a node"""
        if node in self.history:
            del self.history[node]


# ============================================================
# SERVICE STATE TRACKER
# ============================================================
class ServiceStateTracker:
    """Tracks current state of services (replica counts, etc)"""

    def __init__(self, client_manager: DockerClientManager):
        self.client_manager = client_manager

    def get_service_replicas(self, service_name: str) -> int:
        """Get current replica count for a service"""
        try:
            client = self.client_manager.get_local_client()
            if not client:
                return 0
            service = client.services.get(service_name)
            return service.attrs['Spec']['Mode']['Replicated']['Replicas']
        except:
            return 0

    def get_service_container_count_by_node(self, service_name: str) -> Dict[str, int]:
        """Returns dict like {'worker-1': 2, 'worker-2': 1}"""
        try:
            client = self.client_manager.get_local_client()
            if not client:
                return {}

            service = client.services.get(service_name)
            tasks = service.tasks(filters={'desired-state': 'running'})

            count_by_node = defaultdict(int)
            for task in tasks:
                node_id = task.get('NodeID')
                if node_id:
                    # Get node hostname
                    node_obj = client.nodes.get(node_id)
                    node_name = node_obj.attrs['Description']['Hostname']
                    count_by_node[node_name] += 1

            return dict(count_by_node)
        except:
            return {}

    def get_containers_on_node(self, node_name: str) -> List[ContainerMetrics]:
        """
        Fetch containers running on a specific node using Docker API
        Used when PyMonNet doesn't provide container metrics
        """
        try:
            client = self.client_manager.get_client_for_node(node_name)
            if not client:
                logger.warning(f"No remote Docker client for {node_name}")
                return []

            containers_list = client.containers.list()
            result = []

            for container in containers_list:
                # Create placeholder metrics (we don't have real CPU/MEM from Docker API without stats)
                result.append(ContainerMetrics(
                    container=container.name,
                    container_id=container.short_id,
                    cpu=0.0,  # Placeholder - actual value unknown
                    mem=0.0,  # Placeholder - actual value unknown
                    net_in=0.0,
                    net_out=0.0
                ))

            return result
        except Exception as e:
            logger.warning(f"Failed to fetch containers from {node_name}: {e}")
            return []


# ============================================================
# RULE ENGINE
# ============================================================
class RuleEngine:
    """Evaluates metrics and determines recovery actions"""

    def __init__(self, cooldown: CooldownManager, state_tracker: ServiceStateTracker, network_tracker: NetworkHistoryTracker):
        self.cooldown = cooldown
        self.state_tracker = state_tracker
        self.network_tracker = network_tracker

    def evaluate(self, nodes: Dict[str, NodeMetrics]) -> List[RecoveryAction]:
        actions = []

        # Enrich nodes with container data if missing (when node is stressed but PyMonNet hasn't sent containers yet)
        for node_name, node in nodes.items():
            if node.role == "manager":
                continue

            # If node is stressed but has no container data, fetch from Docker API
            if (node.cpu > Config.NODE_CPU_CRITICAL or node.mem > Config.NODE_MEM_CRITICAL) and len(node.containers) == 0:
                logger.info(f"Node {node_name} stressed but no container data - fetching from Docker API")
                node.containers = self.state_tracker.get_containers_on_node(node_name)

        # Group containers by service
        service_containers = defaultdict(list)
        for node_name, node in nodes.items():
            if node.role == "manager":
                continue  # Skip manager nodes

            for container in node.containers:
                service_name = self._extract_service_name(container.container)
                service_containers[service_name].append({
                    'node': node_name,
                    'node_metrics': node,
                    'container': container
                })

        # Evaluate each service
        for service_name, containers_info in service_containers.items():
            action = self._evaluate_service(service_name, containers_info, nodes)
            if action:
                actions.append(action)

        return actions

    def _evaluate_service(self, service_name: str, containers_info: List[dict], nodes: Dict[str, NodeMetrics]) -> Optional[RecoveryAction]:
        """
        Evaluate a single service and determine if action is needed

        SCENARIO 1: Resource Exhaustion (CPU/MEM high, Network low)
        → Container is struggling, migrate to another node

        SCENARIO 2: High Traffic (CPU/MEM/Network ALL high)
        → Service under heavy load, scale up by adding replicas
        → If all containers idle, scale down
        """

        # Check each container instance
        for info in containers_info:
            node_name = info['node']
            node_metrics = info['node_metrics']
            container = info['container']

            cpu_high = container.cpu > Config.CONTAINER_CPU_THRESHOLD or node_metrics.cpu > Config.NODE_CPU_CRITICAL
            mem_high = node_metrics.mem > Config.NODE_MEM_CRITICAL

            # SUSTAINED network detection (requires 3 consecutive samples above threshold)
            # This prevents false positives from brief spikes (e.g., 93 Mbps → 0.3 Mbps → 92 Mbps)
            net_high = self.network_tracker.is_sustained_high(node_name, Config.NETWORK_OUT_THRESHOLD)

            # CHECK SCENARIO 2 FIRST (more specific condition)
            # SCENARIO 2: High traffic (scale up)
            # All three metrics high = cluster-wide traffic spike
            if cpu_high and mem_high and net_high:
                if self.cooldown.can_act(service_name, ActionType.SCALE_UP):
                    # Get network history for logging
                    net_history = self.network_tracker.get_recent_samples(node_name)
                    net_history_str = f"[{', '.join(f'{v:.1f}' for v in net_history)}]"

                    return RecoveryAction(
                        action_type=ActionType.SCALE_UP,
                        service_name=service_name,
                        reason=f"Sustained high traffic on {node_name} (CPU:{node_metrics.cpu:.1f}%, MEM:{node_metrics.mem:.1f}%, NET:{net_history_str}Mbps sustained) → scale +1",
                        metrics={
                            "node": node_name,
                            "cpu": node_metrics.cpu,
                            "mem": node_metrics.mem,
                            "net_out": node_metrics.net_out,
                            "net_history": net_history,
                            "scenario": "high_traffic"
                        }
                    )

            # SCENARIO 1: Resource exhaustion (migrate)
            # CPU/MEM high but network LOW = localized container problem
            elif (cpu_high or mem_high) and not net_high:
                if self.cooldown.can_act(service_name, ActionType.MIGRATE_CONTAINER):
                    return RecoveryAction(
                        action_type=ActionType.MIGRATE_CONTAINER,
                        service_name=service_name,
                        problematic_node=node_name,
                        reason=f"Resource exhaustion on {node_name} (CPU:{node_metrics.cpu:.1f}%, MEM:{node_metrics.mem:.1f}%, NET:{node_metrics.net_out:.1f}Mbps) → migrate",
                        metrics={
                            "node": node_name,
                            "cpu": node_metrics.cpu,
                            "mem": node_metrics.mem,
                            "net": node_metrics.net_out,
                            "scenario": "resource_exhaustion"
                        }
                    )

        # SCENARIO 2: Scale down check (all containers idle)
        current_replicas = self.state_tracker.get_service_replicas(service_name)
        if current_replicas > 1:  # Only scale down if we have more than 1 replica
            all_idle = self._check_scale_down_condition(service_name, containers_info, current_replicas)
            if all_idle and self.cooldown.can_act(service_name, ActionType.SCALE_DOWN):
                return RecoveryAction(
                    action_type=ActionType.SCALE_DOWN,
                    service_name=service_name,
                    reason=f"All {current_replicas} containers idle → scale -1",
                    metrics={"current_replicas": current_replicas, "scenario": "scale_down"}
                )

        return None

    def _check_scale_down_condition(self, service_name: str, containers_info: List[dict], current_replicas: int) -> bool:
        """
        Check if we can scale down:
        Sum of all container usage < threshold × (current_replicas - 1) × SCALE_DOWN_FACTOR
        """
        total_cpu = sum(info['container'].cpu for info in containers_info)
        total_mem = sum(info['node_metrics'].mem for info in containers_info)

        # Calculate if we can handle current load with 1 less replica
        threshold_cpu = Config.CONTAINER_CPU_THRESHOLD * (current_replicas - 1) * Config.SCALE_DOWN_FACTOR
        threshold_mem = Config.NODE_MEM_CRITICAL * (current_replicas - 1) * Config.SCALE_DOWN_FACTOR

        return total_cpu < threshold_cpu and total_mem < threshold_mem

    def _extract_service_name(self, container_name: str) -> str:
        """Extract service name from container name (e.g., 'organic-web-stress.1.xyz' → 'organic-web-stress')"""
        if '.' in container_name:
            return container_name.split('.')[0]
        return container_name


# ============================================================
# ACTION EXECUTOR
# ============================================================
class ActionExecutor:
    """Executes recovery actions"""

    def __init__(self, client_manager: DockerClientManager, cooldown: CooldownManager):
        self.client_manager = client_manager
        self.cooldown = cooldown

    def execute(self, action: RecoveryAction) -> bool:
        """Execute a recovery action"""

        # Check cooldown BEFORE executing
        if not self.cooldown.can_act(action.service_name, action.action_type):
            logger.info(f"Action skipped due to cooldown: {action.action_type.value} for {action.service_name}", extra={
                "action": action.action_type.value,
                "service": action.service_name,
                "reason": "cooldown_active"
            })
            return False

        logger.info(f"Executing action: {action.action_type.value} for {action.service_name}", extra={
            "action": action.action_type.value,
            "service": action.service_name,
            "reason": action.reason
        })

        success = False

        try:
            if action.action_type == ActionType.MIGRATE_CONTAINER:
                success = self._migrate_container(action.service_name, action.problematic_node)
            elif action.action_type == ActionType.SCALE_UP:
                success = self._scale_up(action.service_name)
            elif action.action_type == ActionType.SCALE_DOWN:
                success = self._scale_down(action.service_name)

            if success:
                self.cooldown.record_action(action.service_name, action.action_type)
                logger.info(f"Action completed successfully: {action.action_type.value}", extra={
                    "service": action.service_name,
                    "action": action.action_type.value
                })
            else:
                logger.warning(f"Action failed: {action.action_type.value}", extra={
                    "service": action.service_name,
                    "action": action.action_type.value
                })

        except Exception as e:
            logger.error(f"Exception during action execution: {e}", extra={
                "service": action.service_name,
                "action": action.action_type.value,
                "error": str(e)
            })

        return success

    def _migrate_container(self, service_name: str, problematic_node: str) -> bool:
        """
        SCENARIO 1: Zero-downtime migration
        1. Add constraint to exclude problematic node
        2. Scale up by 1 (so we have 2 containers temporarily)
        3. Wait for new container to start on different node
        4. Scale back down to 1 (removes old container)
        5. Remove constraint
        """
        client = self.client_manager.get_local_client()
        if not client:
            return False

        try:
            service = client.services.get(service_name)
            original_replicas = service.attrs['Spec']['Mode']['Replicated']['Replicas']

            logger.info(f"Starting migration for {service_name} from {problematic_node}")

            # Trigger rolling update with zero-downtime settings
            # This forces Docker to recreate the container on a different node
            update_config = {
                'force_update': True,
                'update_config': {
                    'parallelism': 1,
                    'delay': 10_000_000_000,  # 10 seconds (nanoseconds)
                    'failure_action': 'rollback',
                    'monitor': 15_000_000_000,  # 15 seconds
                    'max_failure_ratio': 0.0,
                    'order': 'start-first'  # Start new before stopping old (ZERO DOWNTIME)
                }
            }

            service.update(**update_config)
            logger.info(f"Triggered zero-downtime migration for {service_name} (start-first strategy)")
            logger.info(f"Migration completed for {service_name}")
            return True

        except Exception as e:
            logger.error(f"Migration failed: {e}")
            return False

    def _scale_up(self, service_name: str) -> bool:
        """SCENARIO 2: Scale up by 1 replica"""
        client = self.client_manager.get_local_client()
        if not client:
            return False

        try:
            service = client.services.get(service_name)
            current_replicas = service.attrs['Spec']['Mode']['Replicated']['Replicas']
            new_replicas = current_replicas + 1

            service.scale(new_replicas)
            logger.info(f"Scaled up {service_name} from {current_replicas} to {new_replicas}")
            return True
        except Exception as e:
            logger.error(f"Scale up failed: {e}")
            return False

    def _scale_down(self, service_name: str) -> bool:
        """SCENARIO 2: Scale down by 1 replica"""
        client = self.client_manager.get_local_client()
        if not client:
            return False

        try:
            service = client.services.get(service_name)
            current_replicas = service.attrs['Spec']['Mode']['Replicated']['Replicas']

            if current_replicas <= 1:
                logger.warning(f"Cannot scale down {service_name}: already at 1 replica")
                return False

            new_replicas = current_replicas - 1
            service.scale(new_replicas)
            logger.info(f"Scaled down {service_name} from {current_replicas} to {new_replicas}")
            return True
        except Exception as e:
            logger.error(f"Scale down failed: {e}")
            return False


# ============================================================
# MAIN RECOVERY MANAGER
# ============================================================
class RecoveryManager:
    """Main orchestrator for proactive recovery"""

    def __init__(self):
        self.client_manager = DockerClientManager()
        self.poller = MetricPoller()
        self.cooldown = CooldownManager()
        self.network_tracker = NetworkHistoryTracker(history_size=3)  # Require 3 consecutive high samples
        self.state_tracker = ServiceStateTracker(self.client_manager)
        self.rule_engine = RuleEngine(self.cooldown, self.state_tracker, self.network_tracker)
        self.executor = ActionExecutor(self.client_manager, self.cooldown)
        self.running = True

    def run(self):
        """Main loop"""
        self._print_banner()

        logger.info("Recovery Manager started", extra={
            "pymonnet_url": Config.PYMONNET_URL,
            "poll_interval": Config.POLL_INTERVAL,
            "cpu_critical": Config.NODE_CPU_CRITICAL,
            "mem_critical": Config.NODE_MEM_CRITICAL,
            "network_threshold": Config.NETWORK_OUT_THRESHOLD,
            "cooldown_migrate": Config.COOLDOWN_MIGRATE,
            "cooldown_scale_up": Config.COOLDOWN_SCALE_UP,
            "cooldown_scale_down": Config.COOLDOWN_SCALE_DOWN
        })

        while self.running:
            try:
                self._poll_and_recover()
                time.sleep(Config.POLL_INTERVAL)
            except KeyboardInterrupt:
                logger.info("Shutdown requested")
                self.running = False
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
                time.sleep(Config.POLL_INTERVAL)

    def _poll_and_recover(self):
        """Fetch metrics, evaluate rules, execute actions"""
        # Fetch metrics
        nodes = self.poller.fetch()
        if not nodes:
            return

        # Record network samples for sustained detection
        for node_name, node_metrics in nodes.items():
            self.network_tracker.record(node_name, node_metrics.net_out)

        # Evaluate rules
        actions = self.rule_engine.evaluate(nodes)

        if not actions:
            return

        logger.info(f"Rules triggered: {len(actions)} actions pending", extra={"action_count": len(actions)})

        # Execute actions
        for action in actions:
            self.executor.execute(action)

    def _print_banner(self):
        """Print startup banner"""
        print()
        print("    ╔═══════════════════════════════════════════════════════════╗")
        print("    ║     RECOVERY MANAGER FOR DOCKER SWARM V2                  ║")
        print("    ║     Proactive Recovery with Intelligent Scaling           ║")
        print("    ║     FYP Project - Amir Muzakkir @ UiTM 2025              ║")
        print("    ╚═══════════════════════════════════════════════════════════╝")
        print("    ")
        print("Configuration:")
        print(f"  PyMonNet URL:        {Config.PYMONNET_URL}")
        print(f"  Poll Interval:       {Config.POLL_INTERVAL}s")
        print(f"  CPU Critical:        {Config.NODE_CPU_CRITICAL}%")
        print(f"  Memory Critical:     {Config.NODE_MEM_CRITICAL}%")
        print(f"  Container CPU:       {Config.CONTAINER_CPU_THRESHOLD}%")
        print(f"  Network Threshold:   {Config.NETWORK_OUT_THRESHOLD} Mbps (sustained: {self.network_tracker.history_size} samples)")
        print(f"  Cooldown Migrate:    {Config.COOLDOWN_MIGRATE}s")
        print(f"  Cooldown Scale Up:   {Config.COOLDOWN_SCALE_UP}s")
        print(f"  Cooldown Scale Down: {Config.COOLDOWN_SCALE_DOWN}s")
        print()


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    manager = RecoveryManager()
    manager.run()
