#!/usr/bin/env bash
# exit on error
set -o errexit

echo "... Installazione Chromium e ChromeDriver ..."
apt-get update -qq
apt-get install -y -qq chromium chromium-driver || \
  apt-get install -y -qq chromium-browser chromium-chromedriver

echo "... Chromium: $(which chromium || which chromium-browser || echo 'non trovato') ..."
echo "... ChromeDriver: $(which chromedriver || echo 'non trovato') ..."

# Installa dipendenze Python
pip install -r requirements.txt
