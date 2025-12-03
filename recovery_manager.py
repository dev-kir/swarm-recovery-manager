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
    NODE_CPU_CRITICAL = float(os.getenv("NODE_CPU_CRITICAL", "75"))  # Critical
    NODE_MEM_WARNING = float(os.getenv("NODE_MEM_WARNING", "70"))
    NODE_MEM_CRITICAL = float(os.getenv("NODE_MEM_CRITICAL", "85"))
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

# ============================================================
# COOLDOWN MANAGER
# ============================================================
class CooldownManager:
    def __init__(self):
        self.last_actions: dict[str, datetime] = {}
        self.restart_counts: dict[str, list[datetime]] = {}

    def can_act(self, target: str, action_type: ActionType = None) -> bool:
        now = datetime.now()
		
		# If action_type is provided, use separate cooldowns
        if action_type == ActionType.RESTART_CONTAINER:
            if target not in self.last_actions:
                return True
            elapsed = (now - self.last_actions[target]).total_seconds()
            return elapsed > Config.COOLDOWN_RESTART
		
        elif action_type == ActionType.SCALE_SERVICE:
            if target not in self.last_actions:
                return True
            elapsed = (now - self.last_actions[target]).total_seconds()
            return elapsed > Config.COOLDOWN_SCALE
		
        elif action_type == ActionType.REDEPLOY_SERVICE:
            if target not in self.last_actions:
                return True
            elapsed = (now - self.last_actions[target]).total_seconds()
            return elapsed > Config.COOLDOWN_REDEPLOY
		
		# Fallback to old behavior
        if target not in self.last_actions:
            return True
        elapsed = (now - self.last_actions[target]).total_seconds()
        return elapsed > Config.COOLDOWN_RESTART
    
    def record_action(self, target: str, action_type: ActionType = None):
        self.last_actions[target] = datetime.now()
    
    def record_restart(self, container_id: str):
        now = datetime.now()
        if container_id not in self.restart_counts:
            self.restart_counts[container_id] = []
        self.restart_counts[container_id].append(now)
        # Clean old entries
        cutoff = now - timedelta(seconds=Config.RESTART_WINDOW_SECONDS)
        self.restart_counts[container_id] = [
            t for t in self.restart_counts[container_id] if t > cutoff
        ]
    
    def get_restart_count(self, container_id: str) -> int:
        if container_id not in self.restart_counts:
            return 0
        now = datetime.now()
        cutoff = now - timedelta(seconds=Config.RESTART_WINDOW_SECONDS)
        self.restart_counts[container_id] = [
            t for t in self.restart_counts[container_id] if t > cutoff
        ]
        return len(self.restart_counts[container_id])
    
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
            stale_action = self._check_stale_node(node)
            if stale_action:
                actions.append(stale_action)
                continue  # Skip other rules for stale nodes
            
            # Rule 2: Check repeated failures (before other rules)
            repeated_action = self._check_repeated_failures(node)
            if repeated_action:
                actions.append(repeated_action)
                continue
            
            # Rule 3: Node high CPU
            if node.cpu > Config.NODE_CPU_THRESHOLD:
                action = self._handle_high_cpu(node)
                if action:
                    actions.append(action)
            
            # Rule 4: Node high memory
            elif node.mem > Config.NODE_MEM_THRESHOLD:
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
            if (node.cpu > 80 and node.net_out > Config.NETWORK_OUT_THRESHOLD 
                and not is_manager):
                action = self._handle_scale_up(node)
                if action:
                    actions.append(action)
        
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
            count = self.cooldown.get_restart_count(container.container_id)
            if count >= Config.MAX_RESTARTS_BEFORE_REDEPLOY:
                service_name = self._extract_service_name(container.container)
                return RecoveryAction(
                    rule_id="REPEATED_FAILURE",
                    action_type=ActionType.REDEPLOY_SERVICE,
                    target_node=node.node,
                    target_container=container.container_id,
                    target_service=service_name,
                    reason=f"Container restarted {count} times in {Config.RESTART_WINDOW_SECONDS}s",
                    metrics={"restart_count": count, "container": container.container}
                )
        return None
    
    def _handle_high_cpu(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        top_container = self._find_top_cpu_container(node)
        if not top_container:
            return None
        
        return RecoveryAction(
            rule_id="NODE_CPU_HIGH",
            action_type=ActionType.RESTART_CONTAINER,
            target_node=node.node,
            target_container=top_container.container_id,
            target_service=self._extract_service_name(top_container.container),
            reason=f"Node CPU {node.cpu}% > {Config.NODE_CPU_THRESHOLD}%",
            metrics={
                "node_cpu": node.cpu,
                "container": top_container.container,
                "container_cpu": top_container.cpu
            }
        )
    
    def _handle_high_memory(self, node: NodeMetrics) -> Optional[RecoveryAction]:
        top_container = self._find_top_memory_container(node)
        if not top_container:
            return None
        
        return RecoveryAction(
            rule_id="NODE_MEM_HIGH",
            action_type=ActionType.RESTART_CONTAINER,
            target_node=node.node,
            target_container=top_container.container_id,
            target_service=self._extract_service_name(top_container.container),
            reason=f"Node Memory {node.mem}% > {Config.NODE_MEM_THRESHOLD}%",
            metrics={
                "node_mem": node.mem,
                "container": top_container.container,
                "container_mem": top_container.mem
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
        top_container = self._find_top_cpu_container(node)
        if not top_container:
            return None
        
        service_name = self._extract_service_name(top_container.container)
        return RecoveryAction(
            rule_id="SCALE_UP",
            action_type=ActionType.SCALE_SERVICE,
            target_node=node.node,
            target_container=None,
            target_service=service_name,
            reason=f"High CPU ({node.cpu}%) + High Network ({node.net_out} Mbps)",
            metrics={
                "node_cpu": node.cpu,
                "node_net_out": node.net_out,
                "service": service_name
            }
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
        
        # Check cooldown
        target_key = action.target_container or action.target_service or action.target_node
        if not self.cooldown.can_act(target_key, action.action_type):
            logger.info(f"Action skipped (cooldown): {action.rule_id} on {target_key}",
                       extra={"extra": {"rule_id": action.rule_id, "target": target_key, 
                                       "status": "cooldown"}})
            return False
        
        start_time = time.time()
        success = False
        
        try:
            if action.action_type == ActionType.RESTART_CONTAINER:
                success = self._restart_container(action.target_node, action.target_container)
                if success:
                    self.cooldown.record_restart(action.target_container)
            
            elif action.action_type == ActionType.REDEPLOY_SERVICE:
                success = self._redeploy_service(action.target_service)
            
            elif action.action_type == ActionType.SCALE_SERVICE:
                success = self._scale_service(action.target_service)
            
            elif action.action_type == ActionType.DRAIN_NODE:
                success = self._drain_node(action.target_node)
            
            duration_ms = (time.time() - start_time) * 1000
            
            if success:
                self.cooldown.record_action(target_key, action.action_type)
            
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
        if not service_name:
            logger.warning("No service name provided for redeploy")
            return False
        client = self.client_manager.get_local_client()
        if not client:
            logger.error("No local Docker client available for redeploy")
            return False
        try:
            service = client.services.get(service_name)
            service.update(force_update=True)
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
        try:
            client = self.client_manager.get_local_client()
            if not client:
                logger.error("No local Docker client available for drain")
                return False
            node = client.nodes.get(node_name)
            spec = node.attrs['Spec']
            spec['Availability'] = 'drain'
            node.update(spec)
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
            logger.info(f"High load detected on nodes: {high_load_nodes}",
                       extra={"extra": {"high_load_nodes": high_load_nodes}})
        
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
