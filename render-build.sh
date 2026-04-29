#!/usr/bin/env bash
# exit on error
set -o errexit

# Cartella locale per Chrome
CHROME_DIR=$(pwd)/chrome-bin
mkdir -p $CHROME_DIR

if [[ ! -d $CHROME_DIR/opt/google/chrome ]]; then
  echo "...Downloading Chrome"
  cd $CHROME_DIR
  wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  dpkg -x google-chrome-stable_current_amd64.deb .
  rm google-chrome-stable_current_amd64.deb
  cd -
else
  echo "...Chrome already installed"
fi

# Install Python dependencies
pip install -r requirements.txt
