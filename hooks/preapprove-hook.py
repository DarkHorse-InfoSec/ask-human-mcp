#!/usr/bin/env python3
"""PreToolUse hook: when AFK is on, route the tool-approval prompt to Slack
and emit Claude Code's permission-decision JSON based on the reply.

Goal: when the operator has flipped AFK on (via "brb"/"afk" cues handled by
afk-trigger-hook.py), this hook intercepts every tool-approval prompt, posts
the pending tool to Slack, and waits for a parseable y/yes/n/no reply. The
returned decision is emitted as
`{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow|deny", ...}}`
on stdout so Claude Code allows or denies the tool autonomously, without
the operator needing to be at the terminal.

When AFK is off, this hook exits 0 silently - Claude Code's normal terminal
permission prompt remains the gate. Same for any failure mode (server down,
timeout, ambiguous reply): exit 0 silent and let the terminal handle it.
That way a misbehaving Slack bridge can never strand the user; the worst
case is "approval falls back to terminal", same as having no hook.

Why Python: matches notify-hook.py's choice (transcript JSON wrangling,
already-installed runtime). Standard library only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
from typing import Any
from urllib.parse import urlparse


def _make_ssl_context() -> "ssl.SSLContext | None":
    """SSL context that verifies against certifi's CA bundle.

    On Windows, ssl.create_default_context() loads the system ROOT store, which
    can still hold the long-expired DST Root CA X3. OpenSSL then builds the chain
    to that expired root and fails ("certificate has expired") even for a valid
    Let's Encrypt leaf - silently breaking every server call in this hook, which
    made AFK read as OFF and stopped routing to Slack. certifi ships only current
    roots and verifies cleanly. Returns None (urllib's default) when certifi is
    absent, so machines without the problem are unaffected.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


_HTTP_SSL_CTX = _make_ssl_context()

# Shared secret for the server's mutating routes (POST /notify, POST /afk).
# Read from ASK_HUMAN_SHARED_SECRET, else from ~/.claude/.ask-human-secret (one
# line, chmod 600 on POSIX). Absence is deliberately not fatal here: the request
# still goes out, the server answers 401, and _auth_hint() explains it. Failing
# closed is the server's job; failing loudly is this hook's.
SECRET_FILE_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_SECRET_FILE", "~/.claude/.ask-human-secret")
)


def _shared_secret() -> str:
    secret = os.environ.get("ASK_HUMAN_SHARED_SECRET", "").strip()
    if secret:
        return secret
    try:
        with open(SECRET_FILE_PATH, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _post_headers() -> dict:
    """JSON content type plus the bearer credential when we have one."""
    headers = {"Content-Type": "application/json"}
    secret = _shared_secret()
    if secret:
        headers["Authorization"] = "Bearer " + secret
    return headers


def _auth_hint(err) -> None:
    """Name a 401/503 out loud. Both are ways the shared secret can be wrong,
    and both otherwise present as 'approvals silently fell back to the
    terminal'."""
    code = getattr(err, "code", None)
    if code == 401:
        sys.stderr.write(
            "[preapprove-hook] server rejected the shared secret (401). Compare "
            f"ASK_HUMAN_SHARED_SECRET / {SECRET_FILE_PATH} with the server's "
            "ASK_HUMAN_SHARED_SECRET.\n"
        )
    elif code == 503:
        sys.stderr.write(
            "[preapprove-hook] server has no shared secret configured (503). Set "
            "ASK_HUMAN_SHARED_SECRET in its env file and restart it.\n"
        )
    else:
        return
    sys.stderr.flush()


NOTIFY_URL = os.environ.get(
    "ASK_HUMAN_NOTIFY_URL", "http://127.0.0.1:8765/notify"
)
AFK_MARKER_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_AFK_MARKER", "~/.claude/.afk")
)
# Auto-expire backstop: an AFK marker older than this is treated as stale and
# cleared, so a forgotten AFK-on can't keep gating tool calls indefinitely.
# Default 8h. Set ASK_HUMAN_AFK_MAX_AGE_SECONDS=0 to disable.
AFK_MAX_AGE_SECONDS = int(os.environ.get("ASK_HUMAN_AFK_MAX_AGE_SECONDS", str(8 * 3600)))
WAIT_SECONDS = int(os.environ.get("ASK_HUMAN_APPROVAL_WAIT_SECONDS", "1800"))
REMINDER_LEAD_SECONDS = int(
    os.environ.get("ASK_HUMAN_APPROVAL_REMINDER_LEAD", "300")
)
# HTTP timeout slightly longer than the server-side wait so the request stays
# open the entire time the server is polling Slack.
HTTP_TIMEOUT = WAIT_SECONDS + 60

# Settings targets for "always" persistence. Cross-machine durability comes from
# writing to a shared settings file; immediate effect for the current session
# comes from also writing live ~/.claude/settings.json. The settings.local.json
# fallback covers the case where the shared file is unavailable.
SHARED_SETTINGS_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_SHARED_SETTINGS", "~/.claude/settings-shared.json")
)
LIVE_SETTINGS_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_LIVE_SETTINGS", "~/.claude/settings.json")
)
LOCAL_SETTINGS_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_LOCAL_SETTINGS", "~/.claude/settings.local.json")
)

# Shell operators that mean "this is a compound command, don't derive a program
# pattern". On hitting any of these, fall back to literal Bash(<full command>).
_SHELL_OPERATORS = ("&&", "||", ";", "|", ">", "<", "`", "$(", ")")


AFK_STATE_URL = os.environ.get(
    "ASK_HUMAN_AFK_URL", "http://127.0.0.1:8765/afk"
)
AFK_STATE_TIMEOUT = float(os.environ.get("ASK_HUMAN_AFK_TIMEOUT", "1.5"))
# Optional short file cache so this (per-tool) reader needn't GET /afk on every
# single tool call. Default 0 = always GET: a Slack/terminal toggle then takes
# effect on the very next tool call (correctness over saving a ~50ms request).
# Set >0 (e.g. 5) to cache for that many seconds if call volume matters; the
# cost is a toggle taking up to that long to be honored.
AFK_CACHE_TTL = float(os.environ.get("ASK_HUMAN_AFK_CACHE_TTL", "0"))
AFK_CACHE_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_AFK_CACHE", "~/.claude/.afk_server_cache")
)


def _server_afk_state() -> bool | None:
    """GET /afk. Returns True/False, or None if the server is unreachable or
    the response is unparseable (caller treats None as 'no signal')."""
    try:
        with urllib.request.urlopen(
            AFK_STATE_URL, timeout=AFK_STATE_TIMEOUT, context=_HTTP_SSL_CTX
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and "afk" in data:
            return bool(data["afk"])
    except Exception:
        return None
    return None


def _server_afk_state_cached() -> bool | None:
    """Server AFK state with a short file cache, so frequent reads don't GET
    every time. Returns True/False, or None when the server is unreachable and
    no fresh cache exists."""
    now = time.time()
    if AFK_CACHE_TTL > 0:
        try:
            with open(AFK_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict) and (now - float(cached.get("ts", 0))) < AFK_CACHE_TTL:
                return bool(cached["afk"])
        except Exception:
            pass
    val = _server_afk_state()
    if val is not None and AFK_CACHE_TTL > 0:
        try:
            with open(AFK_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"afk": val, "ts": now}, f)
        except Exception:
            pass
    return val


def _afk_active() -> bool:
    """AFK is a single coherent state: the SERVER is authoritative (settable
    from the Slack toggle OR a terminal cue), so a tool routes to Slack when AFK
    is on and to the terminal when off, regardless of where it was toggled. The
    local marker is only an offline fallback used when the server is unreachable.
    """
    server = _server_afk_state_cached()
    if server is not None:
        # Mirror server state into the local marker (the offline-fallback cache).
        try:
            exists = os.path.exists(AFK_MARKER_PATH)
            if server and not exists:
                os.makedirs(os.path.dirname(AFK_MARKER_PATH), exist_ok=True)
                open(AFK_MARKER_PATH, "w").close()
            elif not server and exists:
                os.remove(AFK_MARKER_PATH)
        except Exception:
            pass
        return server
    # Server unreachable: fall back to the local marker (last-known state),
    # honoring the auto-expire backstop so a stale marker can't pin AFK on.
    try:
        if not os.path.exists(AFK_MARKER_PATH):
            return False
        if AFK_MAX_AGE_SECONDS > 0:
            if time.time() - os.path.getmtime(AFK_MARKER_PATH) > AFK_MAX_AGE_SECONDS:
                os.remove(AFK_MARKER_PATH)
                return False
        return True
    except Exception:
        return False


def _project_from_cwd(cwd: str | None) -> str | None:
    """Walk up from cwd looking for a repo marker so subdir invocations still
    show the repo name in Slack. Falls back to cwd basename."""
    if not cwd:
        return None
    try:
        path = os.path.abspath(cwd)
    except Exception:
        return None
    seen: set[str] = set()
    while path and path not in seen:
        seen.add(path)
        git_marker = os.path.join(path, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            return os.path.basename(path) or None
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    trimmed = cwd.rstrip("/\\")
    for sep in ("\\", "/"):
        if sep in trimmed:
            return trimmed.rsplit(sep, 1)[-1] or None
    return trimmed or None


def _bash_program(command: str) -> str | None:
    """Extract argv[0] from a shell command for `Bash(<prog> *)` patterns.

    Returns None when the command contains shell operators (chained / piped /
    redirected), so the caller can fall back to a literal pattern. Also strips
    a leading path on the program token so `./scripts/foo.sh` becomes `foo.sh`
    (matching how Claude Code typically renders these calls).
    """
    if not command or not command.strip():
        return None
    if any(op in command for op in _SHELL_OPERATORS):
        return None
    first = command.strip().split(None, 1)[0]
    # Strip path components: /usr/bin/git -> git, ./deploy.sh -> deploy.sh,
    # D:\bin\foo.exe -> foo.exe. Use both separators to be cross-platform.
    for sep in ("/", "\\"):
        if sep in first:
            first = first.rsplit(sep, 1)[-1]
    return first or None


def _dirname_glob(file_path: str) -> str:
    """Return `<dir>/**` for a file path, normalized to forward slashes.

    Claude Code's permission matcher uses gitignore-style globs; forward
    slashes match cross-platform. Drops trailing separators on the dirname
    so we don't emit `path//**`.
    """
    normalized = file_path.replace("\\", "/")
    parts = normalized.rsplit("/", 1)
    if len(parts) == 1:
        # No dir component (e.g. plain "file.txt") - match the file's
        # siblings in cwd. This is rare for absolute paths the assistant uses.
        return "./**"
    parent = parts[0].rstrip("/") or "/"
    return f"{parent}/**"


def _derive_pattern(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    """Derive a Claude Code permissions.allow pattern from a PreToolUse payload.

    Per the `Bash scope` decision 2026-05-11: Bash patterns match by
    program (argv[0]) only - `Bash(git *)`, not `Bash(git push *)`. Chained
    commands (any shell operator) return None: there is no safe program-level
    pattern, so the caller allows the single call without persisting. We do NOT
    fall back to a literal `Bash(<whole command>)` - such a rule only ever
    re-matches that exact string (useless) and, when the command contains
    parens/quotes, is rejected by Claude Code's rule parser (the root cause of
    the /doctor "mismatched parentheses" errors). See investigation 2026-07-05.

    File-op tools derive a directory glob from `file_path` / `path`. MCP tools
    use their fully qualified name. Anything unrecognized falls through to a
    literal `<ToolName>(*)` pattern so the user can edit it later.
    """
    ti = tool_input or {}
    if tool_name == "Bash":
        cmd = str(ti.get("command", "")).strip()
        prog = _bash_program(cmd)
        if prog:
            return f"Bash({prog} *)"
        return None
    if tool_name in {"Read", "Edit", "Write", "NotebookEdit"}:
        fp = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if fp:
            return f"{tool_name}({_dirname_glob(fp)})"
        # No path to scope the rule to. `Read(*)` would persist permission to
        # read EVERY file on the machine off one "always" click - the same
        # over-broad-persist failure as the compound-Bash case above, and the
        # shape of the Bash(*) rules found in 49 project settings files on
        # 2026-09-04. Allow the single call, persist nothing.
        return None
    if tool_name in {"Glob", "Grep"}:
        path = str(ti.get("path") or "")
        if path:
            return f"{tool_name}({_dirname_glob(path + '/x')})"
        return None  # path-less search: same reasoning as above
    if tool_name.startswith("mcp__"):
        return tool_name
    if tool_name:
        return f"{tool_name}(*)"
    # Empty tool name: `"*"` is a rule that allows literally everything, from a
    # payload we could not even identify. Never persist that.
    return None


def _append_allow_pattern(path: str, pattern: str) -> bool:
    """Append `pattern` to `permissions.allow` in the JSON file at `path`.

    No-ops when the pattern is already present. Returns True on a successful
    write (or no-op-already-present), False on any error (missing file,
    unwritable, JSON parse). Caller decides whether each failure is fatal
    (we treat the shared write as best-effort and require only one of the
    writes to succeed).
    """
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        if not isinstance(data, dict):
            return False
        perms = data.setdefault("permissions", {})
        if not isinstance(perms, dict):
            return False
        allow = perms.setdefault("allow", [])
        if not isinstance(allow, list):
            return False
        if pattern in allow:
            return True
        allow.append(pattern)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _persist_allow_pattern(pattern: str) -> list[str]:
    """Dual-write the pattern to settings-shared.json (durable, cross-machine)
    AND live ~/.claude/settings.json (immediate effect this session). Returns
    the list of paths that received the pattern; empty list means nothing was
    written.
    """
    written: list[str] = []
    # Shared SSD target: best-effort. SSD may not be mounted on every machine.
    if _append_allow_pattern(SHARED_SETTINGS_PATH, pattern):
        written.append(SHARED_SETTINGS_PATH)
    # Live target: required for current-session effect. Job D will re-derive
    # this at next session start anyway; without it the user re-prompts mid-session.
    if _append_allow_pattern(LIVE_SETTINGS_PATH, pattern):
        written.append(LIVE_SETTINGS_PATH)
    # If neither shared nor live worked, fall back to settings.local.json so
    # the rule at least sticks for this machine.
    if not written:
        if _append_allow_pattern(LOCAL_SETTINGS_PATH, pattern):
            written.append(LOCAL_SETTINGS_PATH)
    return written


# ---------------------------------------------------------------------------
# Already-allowed short-circuit.
#
# When AFK is on this hook fires for EVERY tool call. Without this check it
# routes even auto-allowed tools to Slack - anything the current permission
# mode would accept (bypassPermissions / dontAsk / acceptEdits, i.e. the
# shift+tab state) and anything covered by an explicit permissions.allow
# pattern. That both spams Slack at every keystroke and defeats the 'yes
# always' loop: a pattern just persisted to permissions.allow would still
# re-prompt on its next matching call. So before contacting Slack we replicate
# Claude Code's auto-allow resolution and, on a match, exit 0 silent - letting
# Claude Code's own flow allow the call with no prompt anywhere. On ANY
# uncertainty we fall through to Slack (safe default: at worst one extra
# approval, never a wrongful silent auto-allow).
# ---------------------------------------------------------------------------

# Permission modes in which Claude Code auto-accepts every tool, so the hook
# must stay out of the way entirely. (bypassPermissions additionally IGNORES
# hook decisions, so routing it to Slack would just hang for the whole wait.)
_AUTO_ACCEPT_ALL_MODES = {"bypassPermissions", "dontAsk"}
# Tools acceptEdits auto-accepts without a dialog (per Claude Code hooks docs).
_ACCEPT_EDITS_TOOLS = {
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    "CreateFolder", "Delete", "Rename",
}
# The "auto" permission mode postdates this hook's mode table (written
# 2026-06-02) and matched NOTHING here, so the mode tier contributed nothing and
# every call outside permissions.allow was routed to Slack. That is the "asked to
# approve everything on Slack" complaint, measured on the MSI 2026-09-04:
# PreToolUse arrives with permission_mode == "auto".
#
# Deliberately NOT added to _AUTO_ACCEPT_ALL_MODES. Auto mode runs its own
# classifier and does still refuse things (observed 2026-09-04: it blocked a
# heredoc rewriting this very file), so "auto" does NOT mean "accepts
# everything"; treating it that way would let arbitrary Bash and MCP calls run
# unseen during exactly the window the operator cannot watch. Instead it silently
# allows reads and edits - whose blast radius is still bounded by the deny
# rules, which Claude Code keeps enforcing on the exit-0 path - and leaves shell
# and MCP calls on the Slack gate.
_AUTO_MODE_TOOLS = {
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    "CreateFolder", "Delete", "Rename",
    "Read", "NotebookRead", "Glob", "Grep",
}
# Tools whose permissions.allow specifier is a gitignore-style path glob.
_PATH_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
# Path tools whose input legitimately omits the path, defaulting to cwd. Without
# this, `Grep(pattern)` with no `path` compared "" against every spec, matched
# nothing, and went to Slack - reads and searches nagging on every call.
_CWD_DEFAULT_TOOLS = {"Glob", "Grep"}


# Recognized shell command separators (per Claude Code's Bash permission docs).
# A compound command is only auto-allowed when EVERY segment is individually
# permitted, so `Bash(safe *)` never green-lights `safe && rm -rf /`.
_BASH_OPERATORS = ("&&", "||", "|&", ";", "|", "&", "\n")


def _settings_files_for(cwd: str | None) -> list[str]:
    """All settings files whose permissions.allow Claude Code unions for a call.

    Claude Code merges allow rules from the user-level files AND the project's
    `.claude/` files - and crucially, "Yes, don't ask again" persists to the
    PROJECT-level `.claude/settings.local.json`, not a user-level file. Project
    settings load from the session's starting directory (not walked up the
    tree), so we read exactly `<cwd>/.claude/`.
    """
    paths = [LIVE_SETTINGS_PATH, LOCAL_SETTINGS_PATH]
    if cwd:
        try:
            base = os.path.abspath(cwd)
            paths.append(os.path.join(base, ".claude", "settings.json"))
            paths.append(os.path.join(base, ".claude", "settings.local.json"))
        except Exception:
            pass
    return paths


def _settings_allow_patterns(cwd: str | None = None) -> list[str]:
    """Collect permissions.allow patterns from every settings file that applies
    to a call in `cwd` (user-level + project-level).

    Best-effort: an unreadable or malformed file contributes nothing (the
    caller then simply routes to Slack, the safe default).
    """
    patterns: list[str] = []
    for path in _settings_files_for(cwd):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            allow = data.get("permissions", {}).get("allow", [])
            if isinstance(allow, list):
                patterns.extend(str(p) for p in allow)
        except (OSError, ValueError, AttributeError):
            continue
    return patterns


def _parse_pattern(pattern: str) -> tuple[str, str | None]:
    """Split a permission pattern `Tool(spec)` into (tool, spec).

    A bare token (no parentheses, e.g. an mcp tool name) returns (token, None);
    a None spec means the whole tool is allowed.
    """
    pattern = pattern.strip()
    if pattern.endswith(")") and "(" in pattern:
        tool, spec = pattern[:-1].split("(", 1)
        return tool.strip(), spec
    return pattern, None


def _glob_body(glob: str) -> str:
    """Regex body for a glob where `*` matches any run of characters (including
    spaces and separators) and everything else is literal. Not anchored."""
    return ".*".join(re.escape(part) for part in glob.split("*"))


def _path_glob_to_regex(glob: str) -> str:
    """Translate a Claude Code permission path glob (gitignore-style) to regex.

    `**` matches across separators (including none); `*` matches within a single
    path segment. Everything else is matched literally.
    """
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < n and glob[i] == "/":  # collapse `**/` into `.*`
                    i += 1
                continue
            out.append("[^/]*")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    return "^" + "".join(out) + "$"


def _path_matches(path: str, spec: str) -> bool:
    """Gitignore-style match of a file path against a permission specifier.

    Bare filenames (no separator) match at any depth, per gitignore semantics
    (`Read(.env)` == `Read(**/.env)`). `~/` is expanded to the home directory.
    Case-insensitive: the allow-list and a tool's path argument can differ in
    case on Windows, and matching loosely here only ever avoids an extra Slack
    prompt (never a wrongful auto-allow of a different file).
    """
    if not path:
        return False
    norm = path.replace("\\", "/")
    spec_norm = spec.replace("\\", "/")
    if spec_norm.startswith("~/"):
        spec_norm = os.path.expanduser(spec_norm).replace("\\", "/")
    elif "/" not in spec_norm:
        # Bare filename matches at any depth.
        spec_norm = "**/" + spec_norm
    try:
        return re.match(_path_glob_to_regex(spec_norm), norm, re.IGNORECASE) is not None
    except re.error:
        return False


def _bash_segments(command: str) -> list[str]:
    """Split a shell command into segments on top-level operators, leaving
    operators that appear INSIDE quotes intact (so an ssh remote command like
    `ssh host "a; b"` is one segment, not three). Quote handling is simple
    (no backslash-escape tracking); a mis-parse only ever over-splits, which
    routes to Slack - never a wrongful auto-allow."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote is not None:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        op = next((o for o in _BASH_OPERATORS if command.startswith(o, i)), None)
        if op is not None:
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf = []
            i += len(op)
            continue
        buf.append(c)
        i += 1
    seg = "".join(buf).strip()
    if seg:
        segments.append(seg)
    return segments


def _bash_segment_matches(segment: str, spec: str) -> bool:
    """Match one operator-free command segment against one Bash rule spec.

    Mirrors Claude Code's Bash grammar (case-sensitive):
      - a trailing `:*` is the prefix-wildcard alias: `ls:*` matches `ls` and
        `ls -la` but NOT `lsof` (a word boundary is required after the prefix);
      - `*` elsewhere matches any run of characters: `curl *`, `git * main`;
      - no wildcard is an exact full-command match.
    """
    if spec.endswith(":*"):
        pattern = "^" + _glob_body(spec[:-2]) + r"(\s.*)?$"
    else:
        pattern = "^" + _glob_body(spec) + "$"
    try:
        return re.match(pattern, segment) is not None
    except re.error:
        return False


def _bash_command_allowed(command: str, specs: list[str]) -> bool:
    """A Bash command is auto-allowed only when EVERY operator-delimited segment
    matches at least one allow spec (Claude Code's shell-operator awareness)."""
    command = command.strip()
    if not command or not specs:
        return False
    segments = _bash_segments(command)
    if not segments:
        return False
    return all(any(_bash_segment_matches(seg, s) for s in specs) for seg in segments)


def _webfetch_matches(tool_input: dict[str, Any], spec: str) -> bool:
    """Match a WebFetch call against a `domain:<host>` spec (host or subdomain)."""
    if not spec.startswith("domain:"):
        return False
    domain = spec[len("domain:"):].strip().lower()
    try:
        host = (urlparse(str(tool_input.get("url") or "")).hostname or "").lower()
    except Exception:
        return False
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def _search_root_covered(root: str, spec: str) -> bool:
    """Is a search rooted at `root` fully covered by path spec `spec`?

    Only trailing-`/**` directory specs can cover a whole subtree, so anything
    else (a bare filename, an extension glob, a mid-path wildcard) returns False
    and the call routes to Slack - the safe default.
    """
    if not root or not spec.endswith("/**"):
        return False
    spec_dir = spec[:-3].replace("\\", "/")
    if spec_dir.startswith("~/"):
        spec_dir = os.path.expanduser(spec_dir).replace("\\", "/")
    if "*" in spec_dir or "?" in spec_dir:
        return False  # wildcard above the root: can't prove containment
    root_norm = root.replace("\\", "/").rstrip("/").lower()
    spec_dir = spec_dir.rstrip("/").lower()
    return root_norm == spec_dir or root_norm.startswith(spec_dir + "/")


def _spec_matches(
    tool_name: str, tool_input: dict[str, Any], spec: str, cwd: str | None = None
) -> bool:
    """Does `spec` (the inside of `Tool(...)`) cover this call's input?

    Bash is handled separately by the caller (it needs all specs at once for
    operator-aware matching); this covers path tools and WebFetch.
    """
    if spec == "*":
        return True
    if tool_name in _PATH_TOOLS:
        path = str(
            tool_input.get("file_path")
            or tool_input.get("notebook_path")
            or tool_input.get("path")
            or ""
        )
        if not path and tool_name in _CWD_DEFAULT_TOOLS and cwd:
            # `Grep(pattern)` with no path searches cwd. That is a search ROOT,
            # not a file, so `_path_matches` is the wrong question: `Grep(d/**)`
            # does not glob-match `d` itself. The right question is whether
            # everything under the root is covered, i.e. is the root at or below
            # the spec's directory. Anything narrower (`Grep(d/src/**)` against
            # a search rooted at `d`) correctly stays gated, because the search
            # would read files the rule does not cover.
            return _search_root_covered(str(cwd), spec)
        return _path_matches(path, spec)
    if tool_name == "WebFetch":
        return _webfetch_matches(tool_input, spec)
    # Unknown tool with a non-'*' spec: don't guess - route to Slack instead.
    return False


def _tool_already_allowed(
    tool_name: str,
    tool_input: dict[str, Any] | None,
    permission_mode: str | None,
    allow_patterns: list[str],
    cwd: str | None = None,
) -> bool:
    """True when Claude Code would auto-allow this call WITHOUT a prompt.

    Two sources: the active permission mode (the shift+tab state) and an
    explicit permissions.allow match. Mirrors Claude Code's own resolution
    closely enough that AFK never downgrades the user's configured permissions.
    """
    if permission_mode in _AUTO_ACCEPT_ALL_MODES:
        return True
    if permission_mode == "acceptEdits" and tool_name in _ACCEPT_EDITS_TOOLS:
        return True
    if permission_mode == "auto" and tool_name in _AUTO_MODE_TOOLS:
        return True
    ti = tool_input or {}
    bash_specs: list[str] = []
    for pattern in allow_patterns:
        ptool, spec = _parse_pattern(pattern)
        if not fnmatch.fnmatchcase(tool_name, ptool):
            continue
        if spec is None or spec == "*":
            return True  # bare tool name or `(*)` -> whole tool allowed
        if tool_name == "Bash":
            bash_specs.append(spec)  # evaluated together (operator-aware) below
            continue
        if _spec_matches(tool_name, ti, spec, cwd):
            return True
    if tool_name == "Bash" and bash_specs:
        return _bash_command_allowed(str(ti.get("command", "")), bash_specs)
    return False


def _emit_decision(decision: str, reason: str) -> None:
    """Write Claude Code's PreToolUse permission-decision JSON to stdout.

    Schema (verified 2026-05-11 against
    https://code.claude.com/docs/en/hooks.md):
        {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" | "deny" | "ask",
            "permissionDecisionReason": "<human-readable string>"
        }}
    """
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def main() -> None:
    raw = sys.stdin.read()
    if not raw:
        sys.exit(0)
    try:
        payload = json.loads(raw)
    except Exception:
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    # AFK gate. At the keyboard, the terminal is the gate - never emit a
    # decision; Claude Code shows the normal prompt.
    if not _afk_active():
        sys.exit(0)

    # AskUserQuestion is a terminal-only widget: its numbered options render in
    # the CLI and do NOT bridge to Slack (not even as a ping). Using it while AFK
    # leaves the operator blind - the session silently blocks on a picker they
    # cannot see.
    # Deny it and steer Claude to ask in prose and end the turn instead, which
    # routes the full question to Slack (Stop event) for a typed thread reply.
    if str(payload.get("tool_name") or "") == "AskUserQuestion":
        sys.stderr.write(
            "[preapprove-hook] AFK on - blocking AskUserQuestion (terminal-only, "
            "no Slack bridge); steering to prose + end-turn.\n"
        )
        sys.stderr.flush()
        _emit_decision(
            "deny",
            "AFK is on and AskUserQuestion is a terminal-only picker that does not "
            "reach Slack. Do NOT use AskUserQuestion now. Instead, write your "
            "question (with the options as a short numbered/bulleted list) as normal "
            "assistant text and end your turn. That posts the full question to Slack, "
            "where the operator replies by typing in the thread.",
        )
        sys.exit(0)

    # Don't let AFK downgrade the user's existing permission setup. If Claude
    # Code would already auto-allow this call - the shift+tab permission mode
    # (bypassPermissions / dontAsk / acceptEdits) or a permissions.allow match -
    # let it through silently instead of routing to Slack. Best-effort: any
    # error here falls through to the Slack path (the safe default).
    try:
        if _tool_already_allowed(
            str(payload.get("tool_name") or ""),
            payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None,
            payload.get("permission_mode"),
            _settings_allow_patterns(payload.get("cwd")),
            payload.get("cwd"),
        ):
            sys.stderr.write(
                f"[preapprove-hook] AFK on but {payload.get('tool_name')!r} is already "
                f"auto-allowed (mode={payload.get('permission_mode')!r}); skipping Slack.\n"
            )
            sys.stderr.flush()
            sys.exit(0)
    except Exception:
        pass

    # Build the request from Claude Code's native PreToolUse payload. We
    # forward tool_name + tool_input verbatim; the server promotes them into
    # its pending_tool dict for Slack block rendering.
    request_payload = {
        "hook_event_name": "PreToolUse",
        "wait_for_reply": True,
        "timeout_seconds": WAIT_SECONDS,
        "reminder_lead_seconds": REMINDER_LEAD_SECONDS,
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "tool_name": payload.get("tool_name"),
        "tool_input": payload.get("tool_input"),
        "tool_use_id": payload.get("tool_use_id"),
    }
    project = _project_from_cwd(payload.get("cwd"))
    if project:
        request_payload["project"] = project

    sys.stderr.write(
        f"[preapprove-hook] AFK on - routing {payload.get('tool_name')} approval to Slack "
        f"(up to {WAIT_SECONDS}s).\n"
    )
    sys.stderr.flush()

    try:
        req = urllib.request.Request(
            NOTIFY_URL,
            data=json.dumps(request_payload).encode("utf-8"),
            headers=_post_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_HTTP_SSL_CTX) as resp:
            data = resp.read().decode("utf-8")
        response = json.loads(data)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        _auth_hint(e)
        sys.stderr.write(
            f"[preapprove-hook] /notify failed ({e}); falling through to "
            f"terminal prompt.\n"
        )
        sys.stderr.flush()
        sys.exit(0)

    status = str(response.get("status") or "")
    if status == "answered":
        decision = str(response.get("decision") or "ask")
        reply = str(response.get("reply") or "").strip()
        if decision == "allow_always":
            # Persist before emitting the decision so the current call is
            # already covered by permissions.allow when Claude Code re-reads
            # settings on the next tool call. We still emit allow for the
            # current call explicitly - settings.json is read lazily and the
            # decision emitted here is the authoritative answer for this prompt.
            pattern = _derive_pattern(
                str(payload.get("tool_name") or ""),
                payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None,
            )
            if pattern is None:
                # Compound/chained command: no safe program-level pattern exists.
                # Honor the approval for THIS call, but persist nothing - a literal
                # whole-command rule would be useless and often malformed. The user
                # re-approves if the command recurs.
                reason = (
                    "AFK Slack 'always' on a compound command - allowed this call; "
                    "not persisting (no safe program-level pattern to grant)."
                )
                sys.stderr.write(
                    "[preapprove-hook] Slack always on compound command - allowing "
                    "once, not persisting a literal rule.\n"
                )
                sys.stderr.flush()
                _emit_decision("allow", reason)
                sys.exit(0)
            written = _persist_allow_pattern(pattern)
            if written:
                reason = (
                    f"AFK Slack 'always' - persisted {pattern!r} to "
                    f"{', '.join(os.path.basename(p) for p in written)}"
                )
                sys.stderr.write(
                    f"[preapprove-hook] Slack always (reply: {reply!r}) - "
                    f"persisted {pattern!r} to {len(written)} file(s).\n"
                )
            else:
                # Failed to persist anywhere - still allow this one call so the
                # user's intent is honored, but flag so they can investigate.
                reason = (
                    f"AFK Slack 'always' - failed to persist {pattern!r} "
                    f"(allow applied to this call only)"
                )
                sys.stderr.write(
                    f"[preapprove-hook] Slack always - persist FAILED for "
                    f"{pattern!r}; treating as one-shot allow.\n"
                )
            sys.stderr.flush()
            _emit_decision("allow", reason)
            sys.exit(0)
        if decision in {"allow", "deny"}:
            reason = f"AFK Slack reply: {reply}" if reply else "AFK Slack approval"
            sys.stderr.write(
                f"[preapprove-hook] Slack {decision} (reply: {reply!r}).\n"
            )
            sys.stderr.flush()
            _emit_decision(decision, reason)
            sys.exit(0)
        # decision was something else (e.g. "ask"): fall through silent.
        sys.exit(0)

    if status == "timeout":
        sys.stderr.write(
            "[preapprove-hook] Approval-wait timed out - falling through to "
            "terminal prompt.\n"
        )
        sys.stderr.flush()
        sys.exit(0)

    # error / unexpected
    sys.stderr.write(
        f"[preapprove-hook] /notify returned status={status!r}; falling through.\n"
    )
    sys.stderr.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
