#!/bin/sh
set -e

mkdir -p "$OPENSTD_DOWNLOAD_DIR"
chown -R appuser:appuser "$OPENSTD_DOWNLOAD_DIR"

exec gosu appuser "$@"
