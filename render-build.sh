#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

echo "... Download Chromium via Playwright nella cartella progetto ..."
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright-browsers
playwright install chromium
echo "... Chromium pronto in $PLAYWRIGHT_BROWSERS_PATH ..."
