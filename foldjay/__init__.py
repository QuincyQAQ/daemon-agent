"""Foldjay Agent: a small, inspectable agent runtime."""
import sys

if sys.version_info < (3, 10):
    raise RuntimeError("Foldjay Agent requires Python 3.10+. Use ./foldjay.sh or /usr/bin/python3.10.")

__version__ = "0.1.0"
