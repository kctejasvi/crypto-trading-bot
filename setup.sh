#!/usr/bin/env bash
# ============================================================
# Setup script for Crypto Trading Bot
# ============================================================
set -euo pipefail

echo "=============================="
echo "  Crypto Trading Bot Setup"
echo "=============================="

# Check Python 3.11+
python_version=$(python3 --version 2>&1 | awk '{print $2}')
major=$(echo "$python_version" | cut -d. -f1)
minor=$(echo "$python_version" | cut -d. -f2)

if [ "$major" -lt 3 ] || ([ "$major" -eq 3 ] && [ "$minor" -lt 11 ]); then
    echo "ERROR: Python 3.11+ required. Found: $python_version"
    exit 1
fi
echo "Python version: $python_version ✓"

# Create virtualenv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtualenv
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip --quiet

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Create necessary directories
mkdir -p logs data

echo ""
echo "=============================="
echo "  Setup complete!"
echo "=============================="
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml — add your Binance API keys"
echo "  2. For paper trading (safe): python run.py"
echo "  3. For live trading:         python run.py --live"
echo ""
echo "Activate the environment first:"
echo "  source venv/bin/activate"
