"""Daemon Agent: a small, inspectable agent runtime."""
import sys

if sys.version_info < (3, 10):
    raise RuntimeError("Daemon Agent requires Python 3.10+. Use ./daemon-agent.sh or /usr/bin/python3.10.")

__version__ = "0.1.0"
