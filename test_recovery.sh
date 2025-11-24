#!/bin/bash
# ============================================================
# Recovery Manager Test Script
# Verify installation and basic functionality
# ============================================================

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_ok() { echo -e "${GREEN}✓${NC} $1"; }
print_fail() { echo -e "${RED}✗${NC} $1"; }
print_info() { echo -e "${YELLOW}➜${NC} $1"; }

echo "=============================================="
echo "Recovery Manager - Installation Test"
echo "=============================================="
echo ""

# Test 1: Python version
print_info "Checking Python version..."
if python3 --version | grep -q "3.1"; then
    print_ok "Python 3.10+ installed"
else
    print_fail "Python 3.10+ required"
fi

# Test 2: Required packages
print_info "Checking required packages..."
python3 -c "import requests" 2>/dev/null && print_ok "requests installed" || print_fail "requests not installed"
python3 -c "import docker" 2>/dev/null && print_ok "docker SDK installed" || print_fail "docker SDK not installed"

# Test 3: Docker socket access
print_info "Checking Docker socket access..."
if [ -S /var/run/docker.sock ]; then
    print_ok "Docker socket exists"
    if docker info > /dev/null 2>&1; then
        print_ok "Docker daemon accessible"
    else
        print_fail "Cannot connect to Docker daemon"
    fi
else
    print_fail "Docker socket not found"
fi

# Test 4: PyMonNet connectivity
PYMONNET_URL="${PYMONNET_URL:-http://192.168.2.50:6969}"
print_info "Testing PyMonNet connectivity ($PYMONNET_URL)..."
if curl -s --max-time 3 "$PYMONNET_URL/nodes" > /dev/null 2>&1; then
    print_ok "PyMonNet server reachable"
    
    # Show current node status
    echo ""
    print_info "Current cluster status:"
    curl -s "$PYMONNET_URL/nodes" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for node, metrics in data.items():
    status = metrics.get('status', 'unknown')
    cpu = metrics.get('cpu', 0)
    mem = metrics.get('mem', 0)
    symbol = '🔴' if status == 'high_load' else '🟢'
    print(f'  {symbol} {node}: CPU={cpu}%, MEM={mem}%, Status={status}')
"
else
    print_fail "Cannot reach PyMonNet server at $PYMONNET_URL"
fi

# Test 5: Configuration file
print_info "Checking configuration..."
if [ -f ".env" ]; then
    print_ok ".env file exists"
else
    print_info ".env file not found (will use defaults)"
fi

# Test 6: Log directory
print_info "Checking log directory..."
LOG_DIR="/var/log"
if [ -w "$LOG_DIR" ]; then
    print_ok "Log directory writable"
else
    print_info "Log directory not writable (will use console only)"
fi

echo ""
echo "=============================================="
echo "Dry Run Test - Rule Evaluation"
echo "=============================================="
echo ""

# Run a single poll cycle
print_info "Running single poll cycle..."
python3 -c "
import os
os.environ.setdefault('PYMONNET_URL', 'http://192.168.2.50:6969')

from recovery_manager import MetricPoller, RuleEngine, CooldownManager

poller = MetricPoller()
cooldown = CooldownManager()
engine = RuleEngine(cooldown)

print('Polling metrics...')
nodes = poller.poll()

if not nodes:
    print('❌ No metrics received')
else:
    print(f'✓ Received metrics for {len(nodes)} nodes')
    
    print('\\nEvaluating rules...')
    actions = engine.evaluate(nodes)
    
    if actions:
        print(f'⚠️  {len(actions)} recovery actions would be triggered:')
        for action in actions:
            print(f'   - {action.rule_id}: {action.action_type.value} on {action.target_node}')
            print(f'     Reason: {action.reason}')
    else:
        print('✓ No recovery actions needed (cluster healthy)')
"

echo ""
echo "=============================================="
echo "Test Complete"
echo "=============================================="
echo ""
print_info "To start the Recovery Manager:"
echo "   python3 recovery_manager.py"
echo ""
print_info "Or with Docker:"
echo "   docker-compose up -d"
echo ""