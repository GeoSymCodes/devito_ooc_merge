#!/usr/bin/env bash

find /app -type f -name '*.pyc' -delete

export PATH=/venv/bin:$PATH
export PYTHONPATH=/app:$PYTHONPATH

exec "$@"
