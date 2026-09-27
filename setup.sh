#!/usr/bin/env sh
# Opens the UAR setup dashboard in your browser (macOS / Linux). Keep the terminal open.
cd "$(dirname "$0")" || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then exec "$py" setup-dashboard/dashboard.py "$@"; fi
done
echo "Python 3.12 or newer is required: https://www.python.org/downloads/"
exit 1
