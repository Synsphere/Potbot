#!/usr/bin/env bash
set -euo pipefail

sources=(
  /etc/apt/apt-mirrors.txt
  /etc/apt/sources.list.d/ubuntu.sources
  /etc/apt/sources.list
)
archive_pattern='https?://(azure\.archive\.ubuntu\.com|[a-z0-9-]+\.ec2\.archive\.ubuntu\.com|archive\.ubuntu\.com)/ubuntu(/|[[:space:]]|$)'
archive_sources=()
for source in "${sources[@]}"; do
  if [[ -f "$source" ]] && grep -Eq "$archive_pattern" "$source"; then
    archive_sources+=("$source")
  fi
done

if (( ${#archive_sources[@]} == 0 )); then
  exit 0
fi

# Probe the same suite the runner needs. APT still verifies Ubuntu's signed metadata.
mirrors=(
  https://archive.ubuntu.com/ubuntu
  https://azure.archive.ubuntu.com/ubuntu
  https://ubuntu.osuosl.org/ubuntu
  https://mirror.clarkson.edu/ubuntu
)
for mirror in "${mirrors[@]}"; do
  if ! curl --fail --silent --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 4 --max-time 8 --output /dev/null "$mirror/dists/noble-updates/InRelease"; then
    continue
  fi
  echo "Using reachable HTTPS Ubuntu archive mirror $mirror."
  sudo sed -Ei "s#$archive_pattern#$mirror\\2#g" "${archive_sources[@]}"
  exit 0
done

echo 'No configured Ubuntu archive mirror served noble-updates.' >&2
exit 1
