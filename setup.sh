#!/bin/bash
# Setup script for log-puller

set -e

echo "======================================"
echo "log-puller Setup"
echo "======================================"
echo

# Check Python version
echo "Checking Python version..."
python3 --version || { echo "Error: Python 3 not found"; exit 1; }
echo "✓ Python 3 found"
echo

# Install dependencies
echo "Installing dependencies..."
pip3 install -r requirements.txt
echo "✓ Dependencies installed"
echo

# Check for rsync
echo "Checking for rsync..."
which rsync > /dev/null || { echo "Error: rsync not found. Please install rsync."; exit 1; }
echo "✓ rsync found"
echo

# Create hosts file if it doesn't exist
if [ ! -f config/hosts ]; then
    echo "Creating config/hosts from example..."
    cp config/hosts.example config/hosts
    echo "✓ Created config/hosts"
    echo "  → Please edit config/hosts with your actual nodes"
else
    echo "✓ config/hosts already exists"
fi
echo

# Check configuration
echo "Validating configuration..."
python3 -c "from src.config_manager import ConfigManager; cm = ConfigManager(); cm.load(); errors = cm.validate(); exit(1 if errors else 0)" 2>/dev/null || {
    echo "⚠ Configuration has issues. Please review config/config.yaml"
}
echo

echo "======================================"
echo "Setup Complete!"
echo "======================================"
echo
echo "Next steps:"
echo "  1. Edit config/hosts with your actual nodes"
echo "  2. Review and customize config/config.yaml"
echo "  3. Test SSH connectivity to your nodes"
echo "  4. Run: ./main.py explore"
echo "  5. Run: ./main.py sync --dry-run"
echo "  6. Run: ./main.py sync"
echo
