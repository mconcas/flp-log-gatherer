#!/usr/bin/env python3
"""
flp-log-gatherer - Main CLI application for collecting logs from heterogeneous nodes

This is a simple wrapper that calls the main CLI implementation from src.cli
For development, you can run this directly: python main.py [command]
For production, install the package: pip install . && flp-log-gatherer [command]
"""
import sys

# Import the main CLI function from src
from src.cli import main

if __name__ == '__main__':
    sys.exit(main())
