#!/usr/bin/env bash
set -euo pipefail

shared_dir="${1:-/opt/martincall/shared}"

if (( EUID != 0 )); then
  echo "Run with sudo so the credential files remain root-owned." >&2
  exit 1
fi

install -d -m 0700 "${shared_dir}" "${shared_dir}/secrets"

read -r -p "IBKR username: " ibkr_username
if [[ ! "${ibkr_username}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "IBKR username contains unsupported characters." >&2
  exit 1
fi

read -r -s -p "IBKR password: " ibkr_password
echo
if [[ -z "${ibkr_password}" || "${ibkr_password}" == *$'\n'* ]]; then
  echo "IBKR password must be non-empty and single-line." >&2
  exit 1
fi

read -r -s -p "Private VNC maintenance password (exactly 8 ASCII letters/digits): " vnc_password
echo
if [[ ! "${vnc_password}" =~ ^[A-Za-z0-9]{8}$ ]]; then
  echo "VNC password must contain exactly 8 ASCII letters/digits." >&2
  exit 1
fi

umask 077
gateway_tmp="$(mktemp "${shared_dir}/gateway.env.XXXXXX")"
ibkr_tmp="$(mktemp "${shared_dir}/secrets/ibkr_password.XXXXXX")"
vnc_tmp="$(mktemp "${shared_dir}/secrets/vnc_password.XXXXXX")"
trap 'rm -f "${gateway_tmp}" "${ibkr_tmp}" "${vnc_tmp}"' EXIT

printf 'TWS_USERID=%s\n' "${ibkr_username}" >"${gateway_tmp}"
printf '%s\n' "${ibkr_password}" >"${ibkr_tmp}"
printf '%s\n' "${vnc_password}" >"${vnc_tmp}"

install -o root -g root -m 0600 "${gateway_tmp}" "${shared_dir}/gateway.env"
# The pinned Gateway image runs as 1000:1000. Compose file-backed secrets keep
# their host ownership, so grant only that process owner read access.
install -o 1000 -g 1000 -m 0600 "${ibkr_tmp}" "${shared_dir}/secrets/ibkr_password"
install -o 1000 -g 1000 -m 0600 "${vnc_tmp}" "${shared_dir}/secrets/vnc_password"

unset ibkr_password vnc_password
echo "IBKR and VNC credentials saved under ${shared_dir}."
