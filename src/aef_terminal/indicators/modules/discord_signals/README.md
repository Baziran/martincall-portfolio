# Discord Signals

This package receives an explicit set of exact Discord channels and one exact
author through the official Discord Desktop local RPC API. It does not use a normal Discord user
token, a self-bot, browser scraping, a bot installed in the source server, or
Discord REST polling.

## Data flow

1. The host companion connects to the Discord Desktop Unix IPC socket.
2. Discord displays an interactive OAuth authorization modal for the scopes
   `rpc`, `identify`, and `messages.read`.
3. The companion subscribes to `MESSAGE_CREATE`, `MESSAGE_UPDATE`, and
   `MESSAGE_DELETE` for every configured exact channel ID.
4. Creates and updates leave the companion only for the configured exact author ID. Deletes from
   an exact configured channel are forwarded to the durable owner. A delete-before-create writes
   a fact-free typed tombstone; an exact replay, stale mutation, or wrong-channel conflict receives
   the authoritative stored outcome without fabricating message facts.
5. Signed typed envelopes are committed to canonical PostgreSQL before they
   enter the terminal's bounded in-memory projection. Updates and deletes are
   durable, and the recent projection is restored after restart.
6. On every RPC session or reconnect, subscriptions open before a bounded `GET_CHANNEL` read. Exact
   returned messages are upserted and events received during that read are replayed afterward in
   order. Discord RPC provides no completeness/pagination fact for this array, so an omitted row is
   never inferred deleted; only explicit `MESSAGE_DELETE` creates a tombstone.
7. A per-page UUIDv4 lease opens Discord IPC only while at least one terminal
   tab has Discord Signals Calc enabled. The last release closes
   IPC; a missed release expires automatically.
8. Rows from every configured channel are deduplicated by exact message ID and
   replayed as one chronological `(published_at, message_id, ordinal)` journal.
   Channel ID remains provenance; it is not a lifecycle partition.
9. The lifecycle reducer keeps zero or one active author position. A new
   different contract is a typed replacement/flip boundary, while an exact
   same-contract entry is a rebroadcast rather than another position.
10. The same projection is shown on every exact instrument ID selected in the
    indicator's chart group.

The chart projection is deliberately based on the selected chart's own bars. It renders typed
entry, add, trim, exit, management, and directional markers at the source message candle, plus
optional advisory trade paths; reported option strikes and premiums remain descriptive facts and
are never transformed into chart prices. Markers stay at two short lines. The complete raw Discord
message is retained in the expanded hover tooltip. **Management**, **Trade paths**, and **Forecast
ticks** are expanded-settings controls only; they are not returned to the compact settings row.
The three marker text-size scales are `0.5`, `0.6`, and `0.7`. The official local-RPC path is the
package's only runtime feed authority.

The terminal retains at most the configured message count per channel and 72 hours in
memory by default, then globally orders the retained union. Canonical PostgreSQL keeps the complete normalized live-message journal for
later parser review. It stores exact identity, text, publish/reply metadata, and source-event
timing, but not the original byte-for-byte Discord payload, attachments, or embed bodies.

## Offline research archive

Historical exports and retired research workflows are external evidence, never runtime authority
or default package context. Consult [the data boundary](../../../../../data/README.md) only when a
task names an artifact or explicitly requires research reproduction.

Stop declarations, moves, and breakeven instructions remain typed management
events. A position is classified as a stop only when the author explicitly
reports the stop execution; declaring a stop does not close a position.

## One-time Discord setup

1. Open <https://discord.com/developers/applications> using the same Discord
   account that runs Discord Desktop.
2. Create an application named `MartinCall Discord Signals`.
3. In **General Information**, copy the **Application ID**. This is the RPC
   client ID.
4. In **OAuth2 / General**, add this exact redirect URL:
   `http://127.0.0.1:8787/discord-rpc`.
5. In **OAuth2 / General**, copy or reset the **Client Secret**. It belongs only
   in the ignored host companion secret file.
6. The application owner is eligible for local development. If Discord reports
   that the account is not an approved tester, open **App Testers**, invite the
   account, and accept the email invitation. If Discord asks for application
   test mode, enable **User Settings / Advanced / Application Test Mode** and
   enter the Application ID.
7. In Discord Desktop, open **User Settings / Advanced** and enable
   **Developer Mode**.
8. Right-click each source signal channel and choose **Copy Channel ID**.
9. Right-click the GAA user and choose **Copy User ID**. Do not use a display
   name as identity.

No bot user needs to be created or invited to the source server.

## Terminal secrets

Generate one bridge secret:

```sh
openssl rand -hex 32
```

Append the following to the existing ignored `secrets/martincall_runtime.env`:

```dotenv
AEF_DISCORD_SIGNALS_ENABLED=true
AEF_DISCORD_SIGNALS_CHANNEL_IDS=<comma-separated exact channel IDs>
AEF_DISCORD_SIGNALS_AUTHOR_ID=<exact GAA user ID>
AEF_DISCORD_SIGNALS_AUTHOR_NAME=GAA
AEF_DISCORD_SIGNALS_BRIDGE_SECRET=<generated bridge secret>
AEF_DISCORD_SIGNALS_RETENTION_HOURS=72
AEF_DISCORD_SIGNALS_HISTORY_LIMIT=500
```

Copy the host-only example and edit it:

```sh
cp secrets/discord_signals_companion.env.example secrets/discord_signals_companion.env
chmod 600 secrets/discord_signals_companion.env
```

Set the following exact values in that file:

- `AEF_DISCORD_RPC_CLIENT_ID`: Developer Portal Application ID.
- `AEF_DISCORD_RPC_CLIENT_SECRET`: Developer Portal OAuth2 Client Secret.
- `AEF_DISCORD_RPC_REDIRECT_URI`: the registered redirect URL above.
- `AEF_DISCORD_SIGNALS_CHANNEL_IDS`: the same comma-separated exact channel IDs.
- `AEF_DISCORD_SIGNALS_AUTHOR_ID`: the same exact GAA user ID.
- `AEF_DISCORD_SIGNALS_BRIDGE_SECRET`: the same generated bridge secret.

The Discord client secret stays in this host-only file. Do not copy it into
`martincall_runtime.env` or `.env`.

## Start

Rebuild and restart the terminal container after changing its runtime secrets.
Keep Discord Desktop running. The repository launcher starts and supervises the
host companion together with the terminal:

```sh
./martincall-server.sh start
./martincall-server.sh discord-status
```

When MartinCall runs on the nettop, keep the terminal URL in the ignored
companion environment file set to `http://127.0.0.1:18000` and use the combined
remote-access lifecycle instead:

```sh
./martincall-server.sh remote-start
./martincall-server.sh remote-status
./martincall-server.sh remote-stop
```

`remote-start` opens the loopback-only SSH forward
`127.0.0.1:18000 -> nettop:127.0.0.1:8000`, verifies the remote readiness
endpoint, and then starts the Discord Desktop RPC companion. The SSH host
defaults to `bazserv-remote`; override it with `MARTINCALL_REMOTE_SSH_HOST` when
required. Keep that terminal open; `Ctrl-C` stops both managed processes.
Tunnel state and logs live outside the repository under
`../data/remote-access/runtime/`.

On the first run Discord Desktop displays the authorization modal. Verify the
application name and approve it. The access token remains only in companion
memory. Restarting the companion may show the modal again.

In the terminal, enable **Discord Signals / Calc** and select the exact SPX, ES,
and SPY instrument IDs in **Chart group**.
The status line changes to `Discord RPC · companion_live` when the connection is
ready.

The companion includes its replay interval in every signed envelope. If three
expected snapshots are missed, the indicator reports `STALE`; the retained
messages remain visible, but the status no longer claims the live feed is
current.

Companion process state is stored outside the repository at
`../data/discord-signals/runtime/companion.pid` and `companion.log`.

`GET_CHANNEL` supplies Discord Desktop's available recent message cache. Its
depth is not guaranteed by Discord. Live events are pushed after subscription;
if the cache does not cover the desired initial two or three days, use the
package preview/import endpoint once for the missing history.
