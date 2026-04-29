#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

echo "... Download Chromium via Playwright ..."
playwright install chromium
echo "... Chromium pronto ..."
