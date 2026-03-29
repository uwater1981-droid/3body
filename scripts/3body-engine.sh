#!/bin/bash
# 3body engine wrapper for launchd — multi-project mode
# Reads projects from ~/.local/lib/3body/projects.json
exec /usr/bin/python3 ~/.local/lib/3body/engine.py
