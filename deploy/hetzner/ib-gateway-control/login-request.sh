#!/bin/bash

set -Eeu -o pipefail

control_fifo="/tmp/martincall-ibkr-login-control/requests"
IFS= read -r request || exit 1
request="${request%$'\r'}"

if [[ "$request" != "START_LOGIN" ]]; then
    printf 'REJECTED\n'
    exit 1
fi

printf 'START_LOGIN\n' >"$control_fifo"
printf 'ACCEPTED\n'
