#!/bin/sh
# Start the private gateway. Secrets stay in files you point at; nothing is read from the shell history here.
set -eu
if [ -z "${LIUGUANG_ANSWERS_ROOT:-}" ] || [ -z "${LIUGUANG_SUBMIT_PASSWORD_FILE:-}" ]; then
  echo "Set LIUGUANG_ANSWERS_ROOT and LIUGUANG_SUBMIT_PASSWORD_FILE" >&2
  exit 2
fi
cd "$(dirname "$0")"
exec python3 server.py
