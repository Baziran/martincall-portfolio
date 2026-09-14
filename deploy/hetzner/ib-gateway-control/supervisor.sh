#!/bin/bash

set -Eeu -o pipefail

control_port="${MARTINCALL_IBKR_LOGIN_CONTROL_PORT:-7463}"
control_dir="/tmp/martincall-ibkr-login-control"
control_fifo="$control_dir/requests"
gateway_pid=""
listener_pid=""

stop_gateway() {
    if [[ -z "$gateway_pid" ]] || ! kill -0 "$gateway_pid" 2>/dev/null; then
        gateway_pid=""
        return
    fi
    printf '.> MartinCall: stopping IB Gateway for an explicit login request.\n'
    kill -TERM "$gateway_pid"
    if wait "$gateway_pid"; then
        :
    else
        printf '.> MartinCall: previous IB Gateway process exited with status %s.\n' "$?"
    fi
    gateway_pid=""
}

remove_autorestart_tokens() {
    local settings_path="${TWS_SETTINGS_PATH:-/home/ibgateway/tws_settings}"
    local token=""
    while IFS= read -r token; do
        printf '.> MartinCall: removing one IBC autorestart token for a cold login.\n'
        rm -f -- "$token"
    done < <(find "$settings_path" -type f -name autorestart -print)
}

start_gateway() {
    local mode="${1:-preserve-session}"
    if [[ "$mode" == "cold-login" ]]; then
        remove_autorestart_tokens
    fi
    printf '.> MartinCall: starting one IB Gateway login attempt.\n'
    /home/ibgateway/scripts/run.sh &
    gateway_pid="$!"
}

start_listener() {
    socat \
        "TCP-LISTEN:${control_port},reuseaddr,fork" \
        "EXEC:/home/ibgateway/martincall-control/login-request.sh,stderr" &
    listener_pid="$!"
}

cleanup() {
    trap - EXIT INT TERM
    if [[ -n "$listener_pid" ]] && kill -0 "$listener_pid" 2>/dev/null; then
        kill -TERM "$listener_pid"
        wait "$listener_pid" 2>/dev/null || true
    fi
    stop_gateway
    rm -f -- "$control_fifo"
}

trap cleanup EXIT INT TERM

mkdir -p "$control_dir"
rm -f -- "$control_fifo"
mkfifo -m 600 "$control_fifo"
exec 3<>"$control_fifo"

start_listener
start_gateway

while :; do
    if ! kill -0 "$listener_pid" 2>/dev/null; then
        wait "$listener_pid" 2>/dev/null || true
        printf '.> MartinCall: login-control listener stopped; restarting it.\n'
        start_listener
    fi
    if [[ -n "$gateway_pid" ]] && ! kill -0 "$gateway_pid" 2>/dev/null; then
        if wait "$gateway_pid"; then
            gateway_status=0
        else
            gateway_status="$?"
        fi
        printf '.> MartinCall: IB Gateway stopped with status %s; waiting for an explicit login request.\n' "$gateway_status"
        gateway_pid=""
    fi

    request=""
    if IFS= read -r -t 1 request <&3 && [[ "$request" == "START_LOGIN" ]]; then
        stop_gateway
        start_gateway cold-login
    fi
done
