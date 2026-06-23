#!/usr/bin/env bash
set -euo pipefail

git pull --ff-only
git submodule sync --recursive
git submodule update --init --recursive