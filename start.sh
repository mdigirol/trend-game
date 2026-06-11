#!/bin/bash
set -e

cd "$(dirname "$0")"

echo ""
echo "  Installing dependencies..."
pip3 install -q -r requirements.txt

echo ""
echo "  ┌──────────────────────────────────────┐"
echo "  │  TrendGame is starting...            │"
echo "  │  Open → http://localhost:5001        │"
echo "  └──────────────────────────────────────┘"
echo ""

python3 app.py
