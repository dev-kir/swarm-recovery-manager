#!/usr/bin/env python3
"""
Recovery Manager for Docker Swarm
Rule-Based Proactive Recovery Mechanism
Author: Amir Muzakkir Bin Md Kamaru Al-Amin
FYP Project - UiTM 2025
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum
from collections import deque  # ADD THIS LINE

import requests
import docker
from docker.tls import TLSConfig


def _parse_host_mapping(raw: str) -> dict[str, str]:
    """Parse env string like 'worker-1=tcp://10.0.0.5:2376,worker-2=tcp://10.0.0.6:2376'."""
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

# ============================================================
# CONFIGURATION (from environment variables)
# ============================================================
# ============================================================
# CONFIGURATION (OPTIMIZED)
# ============================================================
class Config:
    # PyMonNet Server
    PYMONNET_URL = os.getenv("PYMONNET_URL", "http://192.168.2.50:6969")
    POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2"))  # FASTER: 2s instead of 5s
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "3"))
    
    # PREDICTIVE Thresholds (act BEFORE critical)
    NODE_CPU_WARNING = float(os.getenv("NODE_CPU_WARNING", "60"))  # Early warning
    NODE_CPU_CRITICAL = float(os.getenv("NODE_CPU_CRITICAL", "70"))  # Lowered from 75% for earlier action
    NODE_MEM_WARNING = float(os.getenv("NODE_MEM_WARNING", "60"))  # Lowered from 70%
    NODE_MEM_CRITICAL = float(os.getenv("NODE_MEM_CRITICAL", "70"))  # Lowered from 85% - act MUCH earlier!
    CONTAINER_CPU_THRESHOLD = float(os.getenv("CONTAINER_CPU_THRESHOLD", "80"))
    NETWORK_OUT_THRESHOLD = float(os.getenv("NETWORK_OUT_THRESHOLD", "40"))
    STALE_SECONDS = int(os.getenv("STALE_SECONDS", "15"))
    
    # SEPARATE Cooldowns for different actions
    COOLDOWN_RESTART = int(os.getenv("COOLDOWN_RESTART", "30"))
    COOLDOWN_SCALE = int(os.getenv("COOLDOWN_SCALE", "20"))
    COOLDOWN_REDEPLOY = int(os.getenv("COOLDOWN_REDEPLOY", "60"))
    
    # Restart tracking
    MAX_RESTARTS_BEFORE_REDEPLOY = int(os.getenv("MAX_RESTARTS_BEFORE_REDEPLOY", "2"))
    RESTART_WINDOW_SECONDS = int(os.getenv("RESTART_WINDOW_SECONDS", "180"))
    
    # Trend detection
    TREND_WINDOW_SIZE = int(os.getenv("TREND_WINDOW_SIZE", "6"))
    TREND_INCREASE_THRESHOLD = float(os.getenv("TREND_INCREASE_THRESHOLD", "15"))
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("LOG_FILE", "/var/log/recovery-manager.log")
    
    # Remote Docker hosts
    REMOTE_DOCKER_HOSTS = os.getenv("REMOTE_DOCKER_HOSTS", "")
    REMOTE_DOCKER_TLS_VERIFY = os.getenv("REMOTE_DOCKER_TLS_VERIFY", "false").lower() == "true"
    REMOTE_DOCKER_CA_CERT = os.getenv("REMOTE_DOCKER_CA_CERT", "/certs/ca.pem")
    REMOTE_DOCKER_CLIENT_CERT = os.getenv("REMOTE_DOCKER_CLIENT_CERT", "/certs/cert.pem")
    REMOTE_DOCKER_CLIENT_KEY = os.getenv("REMOTE_DOCKER_CLIENT_KEY", "/certs/key.pem")
    REMOTE_DOCKER_HOST_MAP = _parse_host_mapping(REMOTE_DOCKER_HOSTS)
    
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
        if hasattr(record, 'extra'):
            log_obj.update(record.extra)
        return json.dumps(log_obj)

def setup_logging():
    logger = logging.getLogger("recovery_manager")
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(JSONFormatter())
    logger.addHandler(ch)
    
    # File handler (optional)
    try:
        fh = logging.FileHandler(Config.LOG_FILE)
        fh.setFormatter(JSONFormatter())
        logger.addHandler(fh)
    except (PermissionError, FileNotFoundError):
        logger.warning(f"Cannot write to {Config.LOG_FILE}, using console only")
    
    return logger

logger = setup_logging()

# ============================================================
# DATA STRUCTURES
# ============================================================
class ActionType(Enum):
    RESTART_CONTAINER = "restart_container"
    REDEPLOY_SERVICE = "redeploy_service"
    SCALE_SERVICE = "scale_service"
    DRAIN_NODE = "drain_node"


class DockerClientManager:
    """Handles Docker client connections for local manager and remote worker nodes."""
    
    def __init__(self):
        self.local_client = self._init_local_client()
        self.remote_clients: dict[str, docker.DockerClient] = {}
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
            client = self._create_remote_client(node, base_url)
            if client:
                self.remote_clients[node] = client
    
    def _create_remote_client(self, node: str, base_url: str) -> Optional[docker.DockerClient]:
        tls_config = None
        if Config.REMOTE_DOCKER_TLS_VERIFY:
            if not (os.path.exists(Config.REMOTE_DOCKER_CA_CERT) and
                    os.path.exists(Config.REMOTE_DOCKER_CLIENT_CERT) and
                    os.path.exists(Config.REMOTE_DOCKER_CLIENT_KEY)):
                logger.error("TLS verify enabled but certificate files not found")
                return None
            tls_config = TLSConfig(
                client_cert=(Config.REMOTE_DOCKER_CLIENT_CERT, Config.REMOTE_DOCKER_CLIENT_KEY),
                ca_cert=Config.REMOTE_DOCKER_CA_CERT,
                verify=True
            )
        try:
            client = docker.DockerClient(base_url=base_url, tls=tls_config)
            client.ping()
            logger.info(f"Remote Docker client ready for node {node}",
                        extra={"extra": {"node": node, "docker_host": base_url}})
            return client
        except docker.errors.DockerException as e:
            logger.error(f"Failed to connect to remote Docker host {base_url} for node {node}: {e}")
            return None
    
    def get_client_for_node(self, node_name: Optional[str]) -> Optional[docker.DockerClient]:
        if node_name and node_name in self.remote_clients:
            return self.remote_clients[node_name]
        return self.local_client
    
    def get_local_client(self) -> Optional[docker.DockerClient]:
        return self.local_client

@dataclass
class ContainerMetrics:
    container: str
    container_id: str
    cpu: float
    mem: float
    net_in: float
    net_out: float
    timestamp: str

@dataclass
class NodeMetrics:
    node: str
    role: str
    cpu: float
    mem: float
    net_in: float
    net_out: float
    status: str
    timestamp: str
    containers: list[ContainerMetrics] = field(default_factory=list)

@dataclass
class RecoveryAction:
    rule_id: str
    action_type: ActionType
    target_node: str
    target_container: Optional[str]
    target_service: Optional[str]
    reason: str
    metrics: dict
    priority: int = 1  # Higher number = higher priority
    scale_increment: int = 1  # How many replicas to add when scaling up

# ============================================================
# SMART ADAPTIVE COOLDOWN MANAGER
# ============================================================
class CooldownManager:
    """
    Intelligent cooldown that adapts to situation instead of blind time-based waiting.

    Features:
    1. Service-level tracking (not container ID) - allows new containers to be checked
    2. Health-aware: skips cooldown if container is already different/unhealthy
    3. Adaptive cooldown: reduces cooldown if problem persists
    4. Restart loop detection: prevents infinite restart cycles
    """

    def __init__(self):
        # Track by SERVICE name, not container ID
        self.service_last_action: dict[str, datetime] = {}
        self.service_restart_history: dict[str, list[datetime]] = {}
        self.service_last_container: dict[str, str] = {}  # Track which container was last restarted

    def can_act(self, target: str, action_type: ActionType = None,
                service_name: str = None, container_id: str = None,
                container_healthy: bool = None) -> tuple[bool, str]:
        """
        Smart decision on whether to act.

        Returns: (can_act: bool, reason: str)
        """
        now = datetime.now()

        # Use service name for tracking, not container/node ID
        tracking_key = service_name or target

        # RULE 1: If this is a DIFFERENT container than last time, allow immediately!
        # (Container was restarted, now checking the new container)
        if container_id and tracking_key in self.service_last_container:
            if self.service_last_container[tracking_key] != container_id:
                return (True, "different_container")

        # RULE 2: If container is unhealthy/not running, skip cooldown
        if container_healthy is False:
            return (True, "container_unhealthy")

        # RULE 3: Check restart loop protection
        restart_count = self._get_recent_restart_count(tracking_key)

        # If restarted 3+ times in last 60 seconds, prevent restart loop
        if restart_count >= 3:
            if action_type == ActionType.RESTART_CONTAINER:
                # Too many restarts → should escalate to redeploy instead
                return (False, f"restart_loop_detected_{restart_count}_restarts")

        # RULE 4: Adaptive cooldown based on restart history
        if tracking_key not in self.service_last_action:
            return (True, "first_action")

        elapsed = (now - self.service_last_action[tracking_key]).total_seconds()

        # Adaptive cooldown: reduce cooldown if problem persists
        if restart_count == 0:
            # First restart → use full cooldown
            required_cooldown = self._get_cooldown_for_action(action_type)
        elif restart_count == 1:
            # Second restart → reduce cooldown by 50%
            required_cooldown = self._get_cooldown_for_action(action_type) * 0.5
        elif restart_count == 2:
            # Third restart → minimal cooldown (5s)
            required_cooldown = 5
        else:
            # 3+ restarts → blocked by RULE 3 above
            required_cooldown = 999999

        if elapsed > required_cooldown:
            return (True, f"cooldown_expired_{elapsed:.1f}s")
        else:
            remaining = required_cooldown - elapsed
            return (False, f"cooldown_active_{remaining:.1f}s_remaining")

    def _get_cooldown_for_action(self, action_type: ActionType) -> float:
        """Get base cooldown for action type."""
        if action_type == ActionType.RESTART_CONTAINER:
            return Config.COOLDOWN_RESTART
        elif action_type == ActionType.SCALE_SERVICE:
            return Config.COOLDOWN_SCALE
        elif action_type == ActionType.REDEPLOY_SERVICE:
            return Config.COOLDOWN_REDEPLOY
        else:
            return Config.COOLDOWN_RESTART

    def record_action(self, target: str, action_type: ActionType = None,
                     service_name: str = None, container_id: str = None):
        """Record that an action was taken."""
        tracking_key = service_name or target
        self.service_last_action[tracking_key] = datetime.now()

        # Remember which container we acted on
        if container_id:
            self.service_last_container[tracking_key] = container_id

    def record_restart(self, container_id: str, service_name: str = None):
        """Record a restart in history."""
        tracking_key = service_name or container_id
        now = datetime.now()

        if tracking_key not in self.service_restart_history:
            self.service_restart_history[tracking_key] = []

        self.service_restart_history[tracking_key].append(now)

        # Clean old entries (older than 3 minutes)
        cutoff = now - timedelta(seconds=Config.RESTART_WINDOW_SECONDS)
        self.service_restart_history[tracking_key] = [
            t for t in self.service_restart_history[tracking_key] if t > cutoff
        ]

    def _get_recent_restart_count(self, tracking_key: str) -> int:
        """Get number of recent restarts."""
        if tracking_key not in self.service_restart_history:
            return 0

        now = datetime.now()
        cutoff = now - timedelta(seconds=60)  # Last 60 seconds

        recent = [t for t in self.service_restart_history[tracking_key] if t > cutoff]
        return len(recent)

    def get_restart_count(self, container_id: str, service_name: str = None) -> int:
        """Get restart count within RESTART_WINDOW_SECONDS."""
        tracking_key = service_name or container_id
        if tracking_key not in self.service_restart_history:
            return 0

        # Filter to only restarts within the configured window
        now = datetime.now()
        cutoff = now - timedelta(seconds=Config.RESTART_WINDOW_SECONDS)
        recent = [t for t in self.service_restart_history[tracking_key] if t > cutoff]
        return len(recent)
    
# ============================================================
# TREND TRACKER (NEW)
# ============================================================
class TrendTracker:
    """Track metrics over time to predict issues before they become critical."""
    
    def __init__(self):
        from collections import deque
        # Store recent metrics: {node_name: deque([cpu1, cpu2, ...])}
        self.node_cpu_history: dict[str, deque] = {}
        self.node_mem_history: dict[str, deque] = {}
        self.service_load_history: dict[str, deque] = {}
    
    def update(self, nodes: dict[str, NodeMetrics]):
        from collections import deque
        for node_name, node in nodes.items():
            # Track node CPU
            if node_name not in self.node_cpu_history:
                self.node_cpu_history[node_name] = deque(maxlen=Config.TREND_WINDOW_SIZE)
            self.node_cpu_history[node_name].append(node.cpu)
            
            # Track node memory
            if node_name not in self.node_mem_history:
                self.node_mem_history[node_name] = deque(maxlen=Config.TREND_WINDOW_SIZE)
            self.node_mem_history[node_name].append(node.mem)
            
            # Track service load (aggregate container CPU)
            for container in node.containers:
                service = self._extract_service_name(container.container)
                if service not in self.service_load_history:
                    self.service_load_history[service] = deque(maxlen=Config.TREND_WINDOW_SIZE)
                self.service_load_history[service].append(container.cpu)
    
    def is_increasing_rapidly(self, node_name: str, metric_type: str = "cpu") -> bool:
        """Check if a metric is increasing rapidly."""
        history = None
        if metric_type == "cpu":
            history = self.node_cpu_history.get(node_name)
        elif metric_type == "mem":
            history = self.node_mem_history.get(node_name)
        
        if not history or len(history) < 3:
            return False
        
        # Calculate trend: compare recent vs older values
        recent_avg = sum(list(history)[-2:]) / 2
        older_avg = sum(list(history)[:2]) / 2
        
        if older_avg < 10:  # Avoid division by zero and false positives on low load
            return False
        
        increase_pct = ((recent_avg - older_avg) / older_avg) * 100
        return increase_pct > Config.TREND_INCREASE_THRESHOLD
    
    def get_service_trend(self, service_name: str) -> float:
        """Get average load trend for a service."""
        if service_name not in self.service_load_history:
            return 0.0
        history = self.service_load_history[service_name]
        if len(history) < 2:
            return 0.0
        return sum(history) / len(history)
    
    def _extract_service_name(self, container_name: str) -> str:
        parts = container_name.split(".")
        return parts[0] if parts else container_name

# ============================================================
# METRIC POLLER
# ============================================================
class MetricPoller:
    def __init__(self):
        self.url = f"{Config.PYMONNET_URL}/nodes"
    
    def poll(self) -> dict[str, NodeMetrics]:
        try:
            resp = requests.get(self.url, timeout=Config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            return self._parse_metrics(data)
        except requests.RequestException as e:
            logger.error(f"Failed to poll metrics: {e}")
            return {}
    
    def _parse_metrics(self, data: dict) -> dict[str, NodeMetrics]:
        nodes = {}
        for node_name, node_data in data.items():
            containers = []
            if "containers" in node_data:
                for c in node_data["containers"]:
                    containers.append(ContainerMetrics(
                        container=c.get("container", ""),
                        container_id=c.get("container_id", ""),
                        cpu=c.get("cpu", 0.0),
                        mem=c.get("mem", 0.0),
                        net_in=c.get("net_in", 0.0),
                        net_out=c.get("net_out", 0.0),
                        timestamp=c.get("timestamp", "")
                    ))
            
            nodes[node_name] = NodeMetrics(
                node=node_data.get("node", node_name),
                role=node_data.get("role", "worker"),
                cpu=node_data.get("cpu", 0.0),
                mem=node_data.get("mem", 0.0),
                net_in=node_data.get("net_in", 0.0),
                net_out=node_data.get("net_out", 0.0),
                status=node_data.get("status", "normal"),
                timestamp=node_data.get("timestamp", ""),
                containers=containers
            )
        return nodes

# ============================================================
# RULE ENGINE
# ============================================================
class RuleEngine:
    def __init__(self, cooldown: CooldownManager, trend_tracker: TrendTracker):
        self.cooldown = cooldown
        self.trend_tracker = trend_tracker
    
    def evaluate(self, nodes: dict[str, NodeMetrics]) -> list[RecoveryAction]:
        actions = []
        
        for node_name, node in nodes.items():
            # Skip manager nodes for most actions
            is_manager = node.role == "manager"
            
            # Rule 1: Check for stale node
            # stale_action = self._check_stale_node(node)
            # if stale_action:
            #     actions.append(stale_action)
            #     continue  # Skip other rules for stale nodes
            
            # Rule 2: Check repeated failures (before other rules)
            repeated_action = self._check_repeated_failures(node)
            if repeated_action:
                actions.append(repeated_action)
                continue
            
            # Rule 3: Node high CPU
            if node.cpu > Config.NODE_CPU_CRITICAL:
                action = self._handle_high_cpu(node)
                if action:
                    actions.append(action)
            
            # Rule 4: Node high memory
            if node.mem > Config.NODE_MEM_CRITICAL:
                action = self._handle_high_memory(node)
                if action:
                    actions.append(action)
            
            # Rule 5: Container-level high CPU
            for container in node.containers:
                if container.cpu > Config.CONTAINER_CPU_THRESHOLD:
                    action = self._handle_container_high_cpu(node, container)
                    if action:
                        actions.append(action)
            
            # Rule 6: Scale up (CPU + Network)
            if not is_manager:
                scale_action = self._handle_scale_up(node)
                if scale_action:
                    actions.append(scale_action)
                    
            if not is_manager:
                migration_action = self._check_node_migration(node)
                if migration_action:
                    actions.append(migration_action)
        
            # Existing PREDICTIVE_SCALE check
            if not is_manager:
                scale_action = self._handle_scale_up(node)
                if scale_action:
                    actions.append(scale_action)

            # if (node.cpu > 80 and node.net_out > Config.NETWORK_OUT_THRESHOLD 
            #     and not is_manager):
            #     action = self._handle_scale_up(node)
            #     if action:
            #         actions.append(action)
        
		# ADD THIS LINE - Sort by priority (higher = more important)
        actions.sort(key=lambda a: a.priority, reverse=True)
    
        return actions
    
    def _check_stale_node(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        try:
            ts = datetime.fromisoformat(node.timestamp.replace("Z", "+00:00"))
            now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
            elapsed = (now - ts).total_seconds()
            
            if elapsed > Config.STALE_SECONDS:
                return RecoveryAction(
                    rule_id="NODE_STALE",
                    action_type=ActionType.REDEPLOY_SERVICE,
                    target_node=node.node,
                    target_container=None,
                    target_service=None,  # Will redeploy all services
                    reason=f"Node stale for {elapsed:.0f}s (threshold: {Config.STALE_SECONDS}s)",
                    metrics={"last_seen": node.timestamp, "elapsed_seconds": elapsed}
                )
        except (ValueError, TypeError):
            pass
        return None
    
    def _check_repeated_failures(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        for container in node.containers:
            service_name = self._extract_service_name(container.container)
            # Check restart count by service name (not container ID)
            count = self.cooldown.get_restart_count(container.container_id, service_name=service_name)
            if count >= Config.MAX_RESTARTS_BEFORE_REDEPLOY:
                return RecoveryAction(
                    rule_id="REPEATED_FAILURE",
                    action_type=ActionType.REDEPLOY_SERVICE,
                    target_node=node.node,
                    target_container=container.container_id,
                    target_service=service_name,
                    reason=f"Container restarted {count} times in {Config.RESTART_WINDOW_SECONDS}s → escalating to redeploy",
                    metrics={"restart_count": count, "container": container.container},
                    priority=100  # Highest priority - this is critical!
                )
        return None
    
    def _handle_high_cpu(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        """
        SCENARIO DETECTION:
        - If CPU+MEM high but Network LOW → Container problem (Scenario 1)
        - If CPU+MEM+Network ALL high → High traffic (Scenario 2)
        """
        top_container = self._find_top_cpu_container(node)
        if not top_container:
            return None

        service_name = self._extract_service_name(top_container.container)

        # Check if network is also high
        network_high = node.net_out > Config.NETWORK_OUT_THRESHOLD

        if network_high:
            # SCENARIO 2: High traffic → Scale up by adding 1 container
            return RecoveryAction(
                rule_id="HIGH_TRAFFIC_SCALE",
                action_type=ActionType.SCALE_SERVICE,
                target_node=node.node,
                target_container=top_container.container_id,
                target_service=service_name,
                reason=f"High traffic detected (CPU {node.cpu}%, Net {node.net_out:.1f}Mbps) → scale +1",
                metrics={
                    "node_cpu": node.cpu,
                    "node_net": node.net_out,
                    "scenario": "high_traffic"
                },
                scale_increment=1
            )
        else:
            # SCENARIO 1: Container problem → Redeploy to different node (replace, don't add)
            return RecoveryAction(
                rule_id="CONTAINER_PROBLEM",
                action_type=ActionType.REDEPLOY_SERVICE,
                target_node=node.node,
                target_container=top_container.container_id,
                target_service=service_name,
                reason=f"Container problem detected (CPU {node.cpu}%, Net LOW) → redeploy to new node",
                metrics={
                    "node_cpu": node.cpu,
                    "container_cpu": top_container.cpu,
                    "scenario": "container_problem"
                }
            )

    def _handle_high_memory(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        """
        SCENARIO DETECTION:
        - If CPU+MEM high but Network LOW → Container problem (Scenario 1)
        - If CPU+MEM+Network ALL high → High traffic (Scenario 2)
        """
        top_container = self._find_top_memory_container(node)
        if not top_container:
            logger.warning(f"NODE_MEM_HIGH triggered but no container found on {node.node}",
                         extra={"extra": {
                             "node": node.node,
                             "node_mem": node.mem,
                             "container_count": len(node.containers),
                             "containers": [c.container for c in node.containers]
                         }})
            return None

        service_name = self._extract_service_name(top_container.container)

        # Check if network is also high
        network_high = node.net_out > Config.NETWORK_OUT_THRESHOLD

        if network_high:
            # SCENARIO 2: High traffic → Scale up by adding 1 container
            return RecoveryAction(
                rule_id="HIGH_TRAFFIC_SCALE",
                action_type=ActionType.SCALE_SERVICE,
                target_node=node.node,
                target_container=top_container.container_id,
                target_service=service_name,
                reason=f"High traffic detected (MEM {node.mem}%, Net {node.net_out:.1f}Mbps) → scale +1",
                metrics={
                    "node_mem": node.mem,
                    "node_net": node.net_out,
                    "scenario": "high_traffic"
                },
                scale_increment=1
            )
        else:
            # SCENARIO 1: Container problem → Redeploy to different node (replace, don't add)
            return RecoveryAction(
                rule_id="CONTAINER_PROBLEM",
                action_type=ActionType.REDEPLOY_SERVICE,
                target_node=node.node,
                target_container=top_container.container_id,
                target_service=service_name,
                reason=f"Container problem detected (MEM {node.mem}%, Net LOW) → redeploy to new node",
                metrics={
                    "node_mem": node.mem,
                    "container_mem": top_container.mem,
                    "scenario": "container_problem"
                }
            )
    
    def _handle_container_high_cpu(self, node: NodeMetrics, 
                                    container: ContainerMetrics) -> Optional[RecoveryAction]:
        return RecoveryAction(
            rule_id="CONTAINER_CPU_HIGH",
            action_type=ActionType.RESTART_CONTAINER,
            target_node=node.node,
            target_container=container.container_id,
            target_service=self._extract_service_name(container.container),
            reason=f"Container CPU {container.cpu}% > {Config.CONTAINER_CPU_THRESHOLD}%",
            metrics={
                "container": container.container,
                "container_cpu": container.cpu
            }
        )
    
    def _handle_scale_up(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        """INTELLIGENT SCALING: Scale based on load intensity"""

        should_scale = False
        reason_parts = []
        scale_increment = 1  # Default: add 1 replica

		# Calculate load intensity to determine scale amount
        if node.cpu > Config.NODE_CPU_WARNING:  # 60%
            should_scale = True
            reason_parts.append(f"CPU {node.cpu:.1f}%")

            # AGGRESSIVE SCALING: If CPU is very high, scale by 2
            if node.cpu > 80:
                scale_increment = 2
                reason_parts.append("(CRITICAL - scaling +2)")

		# OR if network is high
        if node.net_out > Config.NETWORK_OUT_THRESHOLD:  # 40 Mbps
            should_scale = True
            reason_parts.append(f"Network {node.net_out:.1f}Mbps")

            # AGGRESSIVE SCALING: If network is very high, scale by 2
            if node.net_out > 80:  # 80 Mbps
                scale_increment = max(scale_increment, 2)
                reason_parts.append("(HIGH TRAFFIC - scaling +2)")

        if not should_scale:
            return None

        top_container = self._find_top_cpu_container(node)
        if not top_container:
            return None

        service_name = self._extract_service_name(top_container.container)
        return RecoveryAction(
            rule_id="PREDICTIVE_SCALE",
            action_type=ActionType.SCALE_SERVICE,
            target_node=node.node,
            target_container=None,
            target_service=service_name,
            reason=f"Predictive scale +{scale_increment}: {', '.join(reason_parts)}",
            metrics={
                "node_cpu": node.cpu,
                "node_net_out": node.net_out,
                "service": service_name,
                "scale_increment": scale_increment
            },
            priority=8  # HIGH PRIORITY - execute before restarts
        )
    
    def _find_top_cpu_container(self, node: NodeMetrics) -> Optional[ContainerMetrics]:
        if not node.containers:
            return None
        # Filter out monitoring agents
        app_containers = [c for c in node.containers 
                         if "pymonnet-agent" not in c.container.lower()]
        if not app_containers:
            return None
        return max(app_containers, key=lambda c: c.cpu)
    
    def _find_top_memory_container(self, node: NodeMetrics) -> Optional[ContainerMetrics]:
        if not node.containers:
            return None
        app_containers = [c for c in node.containers 
                         if "pymonnet-agent" not in c.container.lower()]
        if not app_containers:
            return None
        return max(app_containers, key=lambda c: c.mem)
    
    def _extract_service_name(self, container_name: str) -> str:
        # Container name format: service_name.replica.task_id
        # Example: organic-web-stress.1.b9esrfeljlf1csdeu43zguq1r
        parts = container_name.split(".")
        if len(parts) >= 1:
            return parts[0]
        return container_name
    
    def _check_node_migration(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        """
        Detect node problems vs traffic load:
        - High CPU + LOW network = node problem → drain node
        - High CPU + HIGH network = traffic load → already handled by PREDICTIVE_SCALE
        """
    
        # Only check if CPU is critically high
        if node.cpu < Config.NODE_CPU_CRITICAL:  # 75%
            return None
    
        # Check if network is suspiciously LOW for such high CPU
        # (Real traffic would cause proportional network usage)
        if node.net_out < Config.NETWORK_OUT_THRESHOLD * 0.5:  # Less than 20 Mbps
        
            # This looks like a node problem, not traffic
            top_container = self._find_top_cpu_container(node)
            if not top_container:
                return None
        
            service_name = self._extract_service_name(top_container.container)
        
            return RecoveryAction(
                rule_id="NODE_PROBLEM_MIGRATE",
                action_type=ActionType.DRAIN_NODE,
                target_node=node.node,
                target_container=top_container.container_id,
                target_service=service_name,
                reason=f"Suspected node problem: CPU {node.cpu}% but network only {node.net_out:.2f} Mbps",
                metrics={
                    "node_cpu": node.cpu,
                    "node_net_out": node.net_out,
                    "container": top_container.container,
                    "container_cpu": top_container.cpu
                },
                priority=9  # High priority - between REPEATED_FAILURE and NODE_STALE
            )
    
        return None

# ============================================================
# ACTION EXECUTOR
# ============================================================
class ActionExecutor:
    def __init__(self, cooldown: CooldownManager):
        self.cooldown = cooldown
        self.client_manager = DockerClientManager()
    
    def execute(self, action: RecoveryAction) -> bool:
        if not self.client_manager.get_local_client() and not self.client_manager.remote_clients:
            logger.error("No Docker client available for action execution")
            return False

        # Smart cooldown check with context
        target_key = action.target_container or action.target_service or action.target_node

        # Extract service name from container name (e.g., "organic-web-stress.1.abc123" → "organic-web-stress")
        service_name = action.target_service
        if not service_name and action.target_container:
            # Try to extract from container name
            parts = action.target_container.split('.')
            if len(parts) >= 2:
                service_name = parts[0]

        # Check if container is healthy (we'll enhance this later with actual health checks)
        container_healthy = None  # Unknown for now

        can_act, reason = self.cooldown.can_act(
            target_key,
            action_type=action.action_type,
            service_name=service_name,
            container_id=action.target_container,
            container_healthy=container_healthy
        )

        if not can_act:
            logger.info(f"Action skipped: {action.rule_id} on {service_name or target_key}",
                       extra={"extra": {"rule_id": action.rule_id, "target": target_key,
                                       "service": service_name, "reason": reason}})
            return False

        # Log why we're allowing action
        if reason != "first_action":
            logger.info(f"Action allowed: {reason}",
                       extra={"extra": {"rule_id": action.rule_id, "service": service_name,
                                       "reason": reason}})
        
        start_time = time.time()
        success = False
        
        try:
            if action.action_type == ActionType.RESTART_CONTAINER:
                success = self._restart_container(action.target_node, action.target_container)
                if success:
                    self.cooldown.record_restart(action.target_container, service_name=service_name)

            elif action.action_type == ActionType.REDEPLOY_SERVICE:
                success = self._redeploy_service(action.target_service)

            elif action.action_type == ActionType.SCALE_SERVICE:
                # Extract scale_increment from metrics if available
                increment = action.metrics.get("scale_increment", 1)
                success = self._scale_service(action.target_service, increment=increment)

            elif action.action_type == ActionType.DRAIN_NODE:
                success = self._drain_node(action.target_node)

            duration_ms = (time.time() - start_time) * 1000

            if success:
                self.cooldown.record_action(target_key, action.action_type,
                                           service_name=service_name,
                                           container_id=action.target_container)
            
            # Log the action
            log_data = {
                "event": "RECOVERY_ACTION",
                "rule_id": action.rule_id,
                "action": action.action_type.value,
                "node": action.target_node,
                "container": action.target_container,
                "service": action.target_service,
                "reason": action.reason,
                "metrics": action.metrics,
                "result": "success" if success else "failed",
                "duration_ms": round(duration_ms, 2)
            }
            
            if success:
                logger.info(f"Recovery action executed: {action.rule_id}",
                           extra={"extra": log_data})
            else:
                logger.error(f"Recovery action failed: {action.rule_id}",
                            extra={"extra": log_data})
            
            return success
            
        except Exception as e:
            logger.error(f"Exception during action execution: {e}",
                        extra={"extra": {"rule_id": action.rule_id, "error": str(e)}})
            return False
    
    def _restart_container(self, node_name: Optional[str], container_id: str) -> bool:
        if not container_id:
            logger.warning("No container ID provided for restart")
            return False
        client = self.client_manager.get_client_for_node(node_name)
        if not client:
            logger.error(f"No Docker client available for node {node_name}")
            return False
        try:
            container = client.containers.get(container_id)
            container.restart(timeout=10)
            return True
        except docker.errors.NotFound:
            logger.warning(f"Container not found: {container_id}")
            return False
        except docker.errors.APIError as e:
            logger.error(f"Docker API error restarting container: {e}")
            return False
    
    def _redeploy_service(self, service_name: str) -> bool:
        """
        ZERO-DOWNTIME REDEPLOY for Scenario 1 (Container Problem):
        1. Trigger rolling update with proper delays
        2. New container starts on different node
        3. Wait for new container to be healthy
        4. Only then remove old container
        """
        if not service_name:
            logger.warning("No service name provided for redeploy")
            return False
        client = self.client_manager.get_local_client()
        if not client:
            logger.error("No local Docker client available for redeploy")
            return False
        try:
            service = client.services.get(service_name)

            # Configure rolling update with zero-downtime parameters
            update_config = {
                'force_update': True,  # Force new container even if image hasn't changed
                'update_config': {
                    'parallelism': 1,  # Update 1 container at a time
                    'delay': 10_000_000_000,  # 10 second delay between updates (nanoseconds)
                    'failure_action': 'rollback',  # Rollback if new container fails
                    'monitor': 15_000_000_000,  # Monitor for 15 seconds before considering stable
                    'max_failure_ratio': 0.0,  # Don't tolerate any failures
                    'order': 'start-first'  # START new container BEFORE stopping old one (ZERO DOWNTIME!)
                }
            }

            service.update(**update_config)
            logger.info(f"Triggered zero-downtime rolling update for {service_name} (start-first strategy)")
            return True
        except docker.errors.NotFound:
            logger.warning(f"Service not found: {service_name}")
            return False
        except docker.errors.APIError as e:
            logger.error(f"Docker API error redeploying service: {e}")
            return False
    
    def _scale_service(self, service_name: str, increment: int = 1) -> bool:
        if not service_name:
            return False
        client = self.client_manager.get_local_client()
        if not client:
            logger.error("No local Docker client available for scale")
            return False
        try:
            service = client.services.get(service_name)
            current_replicas = service.attrs['Spec']['Mode']['Replicated']['Replicas']
            new_replicas = current_replicas + increment
            service.scale(new_replicas)
            logger.info(f"Scaled {service_name} from {current_replicas} to {new_replicas}")
            return True
        except docker.errors.NotFound:
            logger.warning(f"Service not found: {service_name}")
            return False
        except (docker.errors.APIError, KeyError) as e:
            logger.error(f"Error scaling service: {e}")
            return False
    
    def _drain_node(self, node_name: str) -> bool:
        """Drain node and schedule automatic restoration."""
        try:
            client = self.client_manager.get_local_client()
            if not client:
                logger.error("No local Docker client available for drain")
                return False
        
            node = client.nodes.get(node_name)
            spec = node.attrs['Spec']
            spec['Availability'] = 'drain'
            node.update(spec)
        
            logger.info(f"Node {node_name} drained. Will auto-restore in 5 minutes.")
        
            # Schedule auto-restore (in a separate thread)
            import threading
            def restore_later():
                time.sleep(300)  # Wait 5 minutes
                try:
                    node = client.nodes.get(node_name)
                    spec = node.attrs['Spec']
                    spec['Availability'] = 'active'
                    node.update(spec)
                    logger.info(f"Node {node_name} auto-restored to Active")
                except Exception as e:
                    logger.error(f"Failed to restore node {node_name}: {e}")
        
            threading.Thread(target=restore_later, daemon=True).start()
        
            return True
        except docker.errors.NotFound:
            logger.warning(f"Node not found: {node_name}")
            return False
        except docker.errors.APIError as e:
            logger.error(f"Error draining node: {e}")
            return False

# ============================================================
# MAIN RECOVERY MANAGER (UPDATED) - REPLACE ENTIRE CLASS
# ============================================================
class RecoveryManager:
    def __init__(self):
        self.poller = MetricPoller()
        self.cooldown = CooldownManager()
        self.trend_tracker = TrendTracker()  # NEW: Add trend tracker
        self.rule_engine = RuleEngine(self.cooldown, self.trend_tracker)  # Pass trend tracker
        self.executor = ActionExecutor(self.cooldown)
        self.running = True
    
    def run(self):
        logger.info("Recovery Manager started", extra={"extra": {
            "pymonnet_url": Config.PYMONNET_URL,
            "poll_interval": Config.POLL_INTERVAL,
            "cpu_warning": Config.NODE_CPU_WARNING,
            "cpu_critical": Config.NODE_CPU_CRITICAL,
            "mem_warning": Config.NODE_MEM_WARNING,
            "mem_critical": Config.NODE_MEM_CRITICAL,
            "cooldown_restart": Config.COOLDOWN_RESTART,
            "cooldown_scale": Config.COOLDOWN_SCALE,
            "cooldown_redeploy": Config.COOLDOWN_REDEPLOY
        }})
        
        while self.running:
            try:
                self._poll_and_recover()
            except KeyboardInterrupt:
                logger.info("Shutdown requested")
                self.running = False
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")
            
            time.sleep(Config.POLL_INTERVAL)
        
        logger.info("Recovery Manager stopped")
    
    def _poll_and_recover(self):
        # Step 1: Poll metrics
        nodes = self.poller.poll()
        if not nodes:
            logger.warning("No metrics received from PyMonNet")
            return
        
        # Step 2: Update trend tracker
        self.trend_tracker.update(nodes)
        
        # Log current cluster state
        high_load_nodes = [n for n, m in nodes.items() if m.status == "high_load"]
        if high_load_nodes:
            # Debug: show actual metrics for high load nodes
            metrics_detail = {n: {"cpu": nodes[n].cpu, "mem": nodes[n].mem, "containers": len(nodes[n].containers)}
                            for n in high_load_nodes}
            logger.info(f"High load detected on nodes: {high_load_nodes}",
                       extra={"extra": {"high_load_nodes": high_load_nodes, "metrics": metrics_detail}})
        
        # Step 3: Evaluate rules
        actions = self.rule_engine.evaluate(nodes)
        
        if not actions:
            return
        
        logger.info(f"Rules triggered: {len(actions)} actions pending",
                   extra={"extra": {"action_count": len(actions)}})
        
        # Step 4: Execute actions (already sorted by priority in rule engine)
        for action in actions:
            self.executor.execute(action)
			
# ============================================================
# ENTRY POINT
# ============================================================
def main():
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║     RECOVERY MANAGER FOR DOCKER SWARM                     ║
    ║     Rule-Based Proactive Recovery Mechanism               ║
    ║     FYP Project - Amir Muzakkir @ UiTM 2025              ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    
    print(f"Configuration:")
    print(f"  PyMonNet URL:        {Config.PYMONNET_URL}")
    print(f"  Poll Interval:       {Config.POLL_INTERVAL}s")
    print(f"  CPU Warning:         {Config.NODE_CPU_WARNING}%")
    print(f"  CPU Critical:        {Config.NODE_CPU_CRITICAL}%")
    print(f"  Memory Warning:      {Config.NODE_MEM_WARNING}%")
    print(f"  Memory Critical:     {Config.NODE_MEM_CRITICAL}%")
    print(f"  Container CPU:       {Config.CONTAINER_CPU_THRESHOLD}%")
    print(f"  Network Threshold:   {Config.NETWORK_OUT_THRESHOLD} Mbps")
    print(f"  Cooldown Restart:    {Config.COOLDOWN_RESTART}s")
    print(f"  Cooldown Scale:      {Config.COOLDOWN_SCALE}s")
    print(f"  Cooldown Redeploy:   {Config.COOLDOWN_REDEPLOY}s")
    print()
    
    manager = RecoveryManager()
    manager.run()

if __name__ == "__main__":
    main()
