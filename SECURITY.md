# Security

This service holds a Slack bot token and posts to a channel on your behalf.
That makes the token, not the code, the thing worth protecting.

## Threat model in one line

**A Slack bot token is workspace access.** Anyone who reads it can post as your
bot and read whatever history your granted scopes allow. Treat `.env` the way
you would treat an SSH private key.

## Deployment rules

1. **Never put a production workspace token on an untrusted machine.** Not "just
   for a demo", not "I'll delete it after". If you need to run this somewhere you
   do not fully control, build a throwaway: a new free workspace, one channel, a
   fresh app, and a token that has never touched anything else. Revoke the token
   and delete the workspace when you are done, the same day.

2. **Grant the minimum scopes.** The Slack app manifest in `README.md` declares
   exactly what the server needs. If a scope is not required to post a message
   and poll one channel, do not add it. A bot that can only write to `#one-channel`
   is a much smaller loss than one with `channels:read` across the workspace.

3. **Keep the secret out of the repo and out of the process list.** `.env` is
   gitignored. On a server, put it at `/etc/<service>/env` owned `root:<service>`
   with mode `0640` and load it via systemd `EnvironmentFile`, so it never
   appears in `ps` output or shell history.

4. **Run as a dedicated non-root user.** The provided systemd unit does this and
   additionally sets `ProtectSystem`, `PrivateTmp`, and denies write access to
   the filesystem. See `deploy/ask-human-mcp.service`.

## Why approval state is in memory

The systemd unit denies all disk writes, so AFK state and pending approvals live
in process memory and reset on restart. That is deliberate: the safe default
after an unexpected restart is "not AFK, terminal only", and a durable on-disk
approval cache would be a small database of what you have consented to, sitting
on an internet-facing host, for no real benefit.

The consequence is that a restart clears AFK mode. That is the intended
trade-off, not an oversight.

## Inbound request verification

`POST /slack/interactions` verifies Slack's signature before trusting anything:

- computes `v0=HMAC_SHA256(signing_secret, "v0:<timestamp>:<raw_body>")`
- compares with `hmac.compare_digest`, not `==`, so the check is constant-time
- rejects requests whose timestamp is outside a bounded skew window, which stops
  replay of a previously captured valid request

If `SLACK_SIGNING_SECRET` is unset the route refuses to act rather than
defaulting to trusting the caller.

## Reporting a vulnerability

Please open a GitHub security advisory on this repository rather than a public
issue. Include reproduction steps and the commit you tested.
