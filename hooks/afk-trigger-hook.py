#!/usr/bin/env python3
"""UserPromptSubmit hook: scan the user's prompt for AFK trigger phrases and
toggle the ~/.claude/.afk marker accordingly.

When the marker exists, the Stop hook blocks waiting for a Slack reply when
Claude ends a turn (see notify-hook.py). This script lets the operator flip that
mode by typing natural-language cues in his prompt - no need to break flow
to run `afk.sh on/off` between turns.

Detection philosophy (revised 2026-05-28 after false-ON pings): the asymmetry
matters. A false ON (it thinks the operator left when he didn't) pings Slack while
he's AT the desk - the exact noise this system exists to prevent. A missed ON
is mild (re-type "afk" or tap the Slack toggle). So ON detection is PRECISE,
not greedy:
  - ON fires only for SHORT, cue-dominant messages (<= AFK_MAX_WORDS) or a
    message that opens with a bare shortcode ("afk", "brb"). A trigger word
    buried in a longer sentence ("enable a AFK toggle") does NOT fire.
  - The `_is_meta_context` guard additionally drops cues that are clearly about
    the feature ("the afk hook", "afk.sh", "brb command").
  - Explicit `afk.sh on` and the Slack toggle button always work, regardless of
    wording or length.
OFF detection stays generous (no length/meta gate) because turning AFK off by
mistake is the safe direction - worst case the operator misses a ping while sitting
right there. Earlier history: the original (2026-05-23) "say literally
anything" greedy net caused repeated false-ON incidents (negation, then
meta-discussion); precision replaced recall as the goal.

Mid-turn timing fix: typing "afk" while Claude is mid-turn queues the prompt
until the current turn's Stop has already fired (and read the marker as
absent, so Slack got nothing). To close that gap, this hook fires a one-off
/notify ping the moment AFK transitions off->on, so Slack lights up
immediately regardless of when the next Stop happens.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.request


def _make_ssl_context() -> "ssl.SSLContext | None":
    """SSL context that verifies against certifi's CA bundle.

    On Windows, ssl.create_default_context() loads the system ROOT store, which
    can still hold the long-expired DST Root CA X3. OpenSSL then builds the chain
    to that expired root and fails ("certificate has expired") even for a valid
    Let's Encrypt leaf. certifi ships only current roots and verifies cleanly.
    Returns None (urllib's default) when certifi is absent.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


_HTTP_SSL_CTX = _make_ssl_context()

AFK_MARKER_PATH = os.path.expanduser(
    os.environ.get("ASK_HUMAN_AFK_MARKER", "~/.claude/.afk")
)

NOTIFY_URL = os.environ.get(
    "ASK_HUMAN_NOTIFY_URL", "http://127.0.0.1:8765/notify"
)
# Dedicated state endpoint. POSTing AFK-off here (instead of /notify) is safe
# against a server that predates this feature: it 404s silently rather than
# posting a spurious Slack ping. The new server sets state + refreshes the
# pinned control message.
AFK_STATE_URL = os.environ.get(
    "ASK_HUMAN_AFK_URL", "http://127.0.0.1:8765/afk"
)
ANNOUNCE_TIMEOUT = float(os.environ.get("ASK_HUMAN_AFK_ANNOUNCE_TIMEOUT", "3"))

# Auto AFK-on from natural language fires ONLY for short, cue-dominant messages
# (a real departure announcement is brief: "brb", "afk lunch", "heading out").
# A trigger word buried in a longer sentence is almost always discussion, not a
# departure - and a false ON pings Slack at the desk, the exact thing we're
# avoiding. Messages over this many words don't auto-trigger; use `afk.sh on`,
# the Slack toggle, or a short cue instead. Off detection stays generous (no
# length gate) because turning off by mistake is the safe direction.
AFK_MAX_WORDS = int(os.environ.get("ASK_HUMAN_AFK_MAX_WORDS", "8"))


# Patterns are intentionally generous. Each entry is anchored with \b so a
# pattern like "going to bed" doesn't fire on "going to bedrock". The order
# doesn't matter for ON detection; OFF wins if both are present in the same
# prompt (see main()).
_AFK_ON_PATTERNS = [
    # Abbreviations / chat shortcodes.
    r"\bbrb\b",
    r"\bafk\b",
    r"\bbbl\b",
    r"\bbbiab\b",
    r"\bgtg\b",
    r"\bg2g\b",
    r"\bttyl\b",
    r"\bcyl\b",
    r"\bsyl\b",

    # Stepping / heading / leaving (verb + qualifier keeps false-positive low).
    r"\bstepping (?:away|out|outside|aside|out for|inside)\b",
    r"\bheading (?:out|home|off|away|to bed|to sleep|to lunch|to dinner|to the gym)\b",
    r"\bheaded (?:out|home|off|away|to bed|to sleep|to lunch|to dinner|to the gym)\b",
    r"\bleaving (?:for|now|soon|the (?:desk|keyboard|computer|office|house))\b",
    r"\bgot to go\b",
    r"\bgotta (?:go|run|head out|leave|step (?:away|out)|jet|bounce|dip)\b",
    r"\bneed to (?:go|run|head out|leave|step (?:away|out))\b",
    r"\bhave to (?:go|run|head out|leave|step (?:away|out))\b",
    r"\btime to (?:go|run|head out|leave)\b",
    r"\bduty calls\b",
    r"\bbouncing\b",
    r"\bdipping out\b",

    # "Be back" / "back in N" / "back later".
    r"\bbe right back\b",
    r"\bbe back (?:in|soon|later|shortly|tomorrow|tonight|in a (?:bit|few|while|moment|minute))\b",
    r"\bback later\b",
    r"\bback in (?:a (?:bit|sec|moment|minute|few|couple|while)|\d+\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|hr|hrs|hour|hours)?|(?:a|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|sixty)\s*(?:sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours))\b",
    r"\bback (?:tomorrow|tonight|in the (?:morning|afternoon|evening))\b",

    # Sleep / end-of-day.
    r"\bgoing to (?:bed|sleep)\b",
    r"\bheading (?:to bed|to sleep)\b",
    r"\b(?:bed ?time|bedtime)\b",
    r"\bgetting (?:some )?sleep\b",
    r"\bturning in(?: for the night)?\b",
    r"\bgoodnight\b",
    r"\bgood night\b",
    r"\b(?:going to|gonna) (?:nap|crash|hit the sack|hit the hay|catch some Zs?)\b",
    r"\btaking a nap\b",

    # Food.
    r"\b(?:going to|gonna|going for|grabbing) (?:lunch|dinner|breakfast|brunch|food|a bite|a snack|coffee|a coffee|a meal|some food)\b",
    r"\b(?:going to|gonna) eat\b",
    r"\b(?:lunch|dinner|breakfast|brunch) (?:time|break|now)\b",
    r"\b(?:eating|having) (?:lunch|dinner|breakfast|brunch)(?: now)?\b",
    r"\bfood time\b",
    r"\btime to eat\b",

    # Outdoor / outings / errands.
    r"\bgoing outside\b",
    r"\bgoing (?:out|home|away)\b",
    r"\bgoing for a (?:walk|run|drive|swim|hike|ride|smoke|break|bike ride|jog)\b",
    r"\bgoing to (?:the )?(?:gym|doctor|dentist|store|mall|park|beach|movies|theater|cinema|library|school|college|university|airport|hospital|bank|barber|salon|pharmacy|grocery|pool|gas station|garage|kid'?s school)\b",
    r"\bheading to (?:the )?(?:gym|doctor|dentist|store|mall|park|beach|movies|theater|cinema|library|school|college|university|airport|hospital|bank|barber|salon|pharmacy|grocery|pool|gas station|garage|kid'?s school)\b",
    r"\b(?:got|have) (?:a |an )?(?:appointment|errand)\b",
    r"\brunning (?:errands|to the store|out to)\b",
    r"\bpicking up (?:my |the )?(?:kid|kids|son|daughter|wife|husband|partner|order|food|car)\b",
    r"\bdropping off (?:my |the )?(?:kid|kids|son|daughter|wife|husband|partner|car)\b",
    r"\b(?:doing|on) (?:the )?school run\b",

    # Family / people (high-confidence away signals).
    r"\b(?:spending|spend|gonna spend|going to spend) time (?:with|at)\b",
    r"\b(?:family|kid|kids|wife|husband|partner|spouse) (?:time|stuff)\b",
    r"\bwith (?:my )?(?:family|wife|husband|partner|spouse|kid|kids|children|son|daughter|baby|child)\b",
    r"\bplaying with (?:my |the )?(?:kid|kids|children|son|daughter|dog|cat|baby)\b",
    r"\bkid (?:duty|stuff|time)\b",

    # Personal care / break.
    r"\b(?:taking|going for|going to take|jumping in|hopping in) (?:a |the )?(?:shower|bath)\b",
    r"\b(?:going to|gonna) (?:shower|bathe|exercise|work ?out|cook|clean|do laundry|run errands)\b",
    r"\bshower (?:time|now)\b",
    r"\b(?:bathroom|restroom) (?:break|run)\b",
    r"\bbio break\b",
    r"\btaking a break\b",
    r"\btaking five\b",
    r"\bsmoke break\b",
    r"\bcoffee break\b",

    # Meetings / calls (work-AFK).
    r"\b(?:joining|hopping (?:on|into)|jumping (?:on|into)) (?:a |the )?(?:call|meeting|stand[\s-]?up|sync|huddle|interview|1:1|one[\s-]on[\s-]one)\b",
    r"\b(?:call|meeting|stand[\s-]?up|sync|huddle|interview|1:1) (?:in (?:\d+|a few|a couple|five|ten|fifteen|twenty|thirty)\s*(?:min|mins|minutes)?|started|starting (?:now|soon)|now)\b",
    r"\bon a call\b",
    r"\bin a meeting\b",

    # Generic away-from-keyboard / sign-off phrases.
    r"\baway from (?:the )?(?:keyboard|desk|computer|pc|machine)\b",
    r"\bleaving (?:the )?(?:keyboard|desk|computer|pc|machine)\b",
    r"\b(?:going|gonna go) offline\b",
    r"\b(?:logging|signing) (?:off|out)\b",
    r"\bsee (?:you|ya) (?:later|tomorrow|soon|tonight)\b",
    r"\bcatch (?:you|ya) later\b",
    r"\btalk (?:to you )?(?:later|soon|tomorrow|tonight)\b",
    r"\bpeace out\b",
    r"\bafk for\b",
]

# Match these BEFORE the "on" patterns so e.g. "i'm back" isn't read as a bare
# "back" -> AFK on. The OFF triggers always win when both are present.
_AFK_OFF_PATTERNS = [
    r"\bi'?m back\b",
    r"\bim back\b",
    r"\bi am back\b",
    # First-person-plural returns: "we're back", "we are back online". The
    # plural form is common when reporting that a session/connection resumed,
    # and was missing — "We are back online" stranded the user in Slack-only
    # mode because neither "we are back" nor "online" was recognized.
    r"\bwe'?re back\b",
    r"\bwe are back\b",
    r"\bback online\b",
    r"\b(?:we'?re|we are|i'?m|im|i am)\s+(?:back\s+)?online\b",
    r"\bonline (?:again|now)\b",
    r"\bi'?m here\b",
    r"\bim here\b",
    r"\bi am here\b",
    r"\bback now\b",
    r"\bhere now\b",
    r"\bback at (?:it|my desk|the (?:desk|keyboard|computer))\b",
    # Presence at the workstation, with or without a leading "back". Covers
    # "I'm at the keyboard", "at my desk now", "back at my computer". These are
    # specific enough to stay clear of incidental mentions (the desk/keyboard
    # nouns rarely appear in unrelated prose), and missing them is exactly what
    # stranded a returned user in Slack-only mode.
    r"\b(?:back )?at (?:the|my) (?:keyboard|desk|computer|pc|machine|laptop|workstation)\b",
    r"\bin front of (?:the|my) (?:keyboard|computer|pc|screen|machine|laptop)\b",
    r"\bback (?:to|on) (?:work|it|the keyboard|my desk|my computer)\b",
    r"\bat the helm\b",
    r"\b(?:ok|okay|alright|all right),?\s+(?:i'?m )?back\b",
    r"\bsitting (?:back )?down\b",
    r"\b(?:i'?m |i am )?returning\b",
    r"\breturned\b",
    r"\bgood morning\b",
    # Start-anchored bare "back" so a prompt that opens with "Back" or "Back,
    # go" flips AFK off. Negative lookahead excludes the AWAY phrasings:
    # "Back in 5", "Back later", "Back tomorrow", etc. should be ON, not OFF.
    r"^back\b(?!\s+(?:in|later|tomorrow|tonight|to|home|out|away|on|up|after))",
]


_NEGATION_WORDS = (
    r"not", r"no", r"never", r"none",
    r"isn'?t", r"wasn'?t", r"aren'?t", r"weren'?t",
    r"won'?t", r"wouldn'?t", r"shouldn'?t", r"couldn'?t",
    r"don'?t", r"doesn'?t", r"didn'?t",
    r"ain'?t", r"can'?t", r"cannot",
    r"haven'?t", r"hasn'?t", r"hadn'?t",
)
_NEGATION_RE = re.compile(
    r"\b(?:" + "|".join(_NEGATION_WORDS) + r")\b",
    re.IGNORECASE,
)


def _is_negated(text: str, match_start: int) -> bool:
    """True when the position is inside a negated clause.

    Scans the ~40 chars preceding `match_start` for a negation word ("not",
    "no", "isn't", ...) within the same clause (no sentence-ending punctuation
    between negation and the match). "I am not AFK" -> negated; "AFK soon, not
    leaving yet" -> not negated for "AFK" because the negation comes after.
    """
    look_back = max(0, match_start - 40)
    window = text[look_back:match_start]
    for sep in (".", "!", "?", ";"):
        boundary = window.rfind(sep)
        if boundary != -1:
            window = window[boundary + 1:]
    return bool(_NEGATION_RE.search(window))


# Nouns that, right after a cue, mean the user is talking ABOUT the AFK feature
# rather than announcing a departure: "afk toggle", "afk button", "brb command".
_FEATURE_NOUNS = (
    "toggle", "toggling", "button", "buttons", "feature", "features", "mode",
    "state", "status", "hook", "hooks", "marker", "control", "controls",
    "script", "scripts", "detection", "detector", "pattern", "patterns",
    "trigger", "triggers", "triggering", "system", "bridge", "file", "files",
    "thing", "things", "stuff", "setting", "settings", "setup", "logic", "code",
    "function", "message", "messages", "announcement", "announcements", "ping",
    "pings", "pinging", "notification", "notifications", "flow", "cue", "cues",
    "word", "words", "phrase", "phrases", "reply", "replies", "prompt",
    "prompts", "command", "commands", "endpoint", "behavior", "behaviour",
    # Coding/identifier nouns: a cue word used as an identifier ("afk branch",
    # "brb repo") is discussion, not a departure. the operator works in code all day,
    # so these are common.
    "branch", "branches", "repo", "repos", "pr", "prs", "commit", "commits",
    "issue", "issues", "ticket", "tickets", "tag", "tags", "var", "vars",
    "variable", "variables", "param", "params", "flag", "flags", "env",
)
_META_AFTER_RE = re.compile(
    r"^\W*(?:" + "|".join(_FEATURE_NOUNS) + r")\b", re.IGNORECASE
)
# Determiner/article immediately before a cue also signals a noun phrase about
# the feature: "the afk", "a AFK toggle", "this brb logic", "your afk hook".
_DETERMINER_BEFORE_RE = re.compile(
    r"\b(?:the|a|an|this|that|these|those|your|my|our|its|his|her|their)\s+$",
    re.IGNORECASE,
)


def _is_meta_context(text: str, start: int, end: int) -> bool:
    """True when an ON cue is the user discussing the AFK feature, not stepping
    away. Suppresses triggers like "enable a AFK toggle", "the afk hook",
    "afk.sh" so talking ABOUT AFK doesn't flip it on (and ping Slack). Departure
    cues ("brb", "afk", "going afk now") have no feature noun / determiner and
    still fire.
    """
    after = text[end : end + 24]
    if _META_AFTER_RE.match(after):
        return True
    if text[end : end + 3].lower() == ".sh":  # "afk.sh"
        return True
    before = text[max(0, start - 24) : start]
    if _DETERMINER_BEFORE_RE.search(before):
        return True
    return False


# A message that OPENS with a PUNCTUATION-BOUNDED departure shortcode ("afk",
# "afk!", "brb, ...") is a clear sign-off even if it then runs long, so it
# bypasses the word-count gate. The shortcode must be alone or followed by
# punctuation - NOT by a bare word, so "afk branch needs work" / "brb command
# is broken" do NOT bypass (those are discussion, gated by length + meta).
_STARTS_WITH_CUE_RE = re.compile(
    r"^\s*(?:brb|afk|bbl|bbiab|gtg|g2g|ttyl|cyl|syl)\s*(?:[-,.!?:;)]|$)",
    re.IGNORECASE,
)


def _is_departure_message(text: str) -> bool:
    """True when the prompt is short/cue-dominant enough to be a genuine
    departure announcement (vs. a longer sentence that merely contains a trigger
    word). Gates ON cues only; OFF stays generous."""
    if _STARTS_WITH_CUE_RE.match(text):
        return True
    return len(text.split()) <= AFK_MAX_WORDS


def _earliest_match(patterns: list[str], text: str) -> tuple[int, str] | None:
    """Return (start_position, matched_text) for the EARLIEST-occurring match
    across all patterns, or None. Used to decide OFF-vs-ON precedence by
    temporal position in the prompt: 'I'm back, but going to bed' has the
    OFF match at pos 0 and ON match later; the user's latest intent (going
    to bed) wins because it appears later in the sentence.
    """
    best: tuple[int, str] | None = None
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(0))
    return best


def _earliest_non_negated_on_match(
    patterns: list[str], text: str
) -> tuple[int, str] | None:
    """Like `_earliest_match` but skips ON matches sitting inside a negated
    clause. 'I am not AFK' -> None; 'I am not back, going to bed' -> matches
    'going to bed'. Applied only to ON patterns; OFF patterns are
    intentionally not negation-aware because 'not back' is still away-ish.
    """
    best: tuple[int, str] | None = None
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            if _is_negated(text, m.start()):
                continue
            # Skip cues that are the user discussing the AFK feature rather than
            # announcing a departure ("enable a AFK toggle", "the afk hook").
            if _is_meta_context(text, m.start(), m.end()):
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), m.group(0))
            break
    return best


def _set_afk(state: bool) -> bool:
    """Create or remove the marker file. Returns True iff state changed."""
    was_on = False
    try:
        was_on = os.path.exists(AFK_MARKER_PATH)
    except Exception:
        pass
    if state == was_on:
        return False
    try:
        os.makedirs(os.path.dirname(AFK_MARKER_PATH), exist_ok=True)
    except Exception:
        return False
    try:
        if state:
            with open(AFK_MARKER_PATH, "w", encoding="utf-8") as f:
                f.write("")
        else:
            if os.path.exists(AFK_MARKER_PATH):
                os.remove(AFK_MARKER_PATH)
    except Exception:
        return False
    return True


def _emit_status(state: str, trigger: str) -> None:
    """Stderr summary so the human-visible terminal mirror shows what flipped.

    Stderr (not stdout) - UserPromptSubmit stdout is fed back to the model as
    additional context, and we don't want the trigger detection bleeding into
    the conversation.
    """
    icon = "[afk on]" if state == "on" else "[afk off]"
    msg = (
        f"{icon} matched '{trigger}' - Slack is now the active channel"
        if state == "on"
        else f"{icon} matched '{trigger}' - terminal-only mode"
    )
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _push_afk_state(state: bool, trigger: str, payload: dict) -> None:
    """Sync the server's AFK state with a terminal cue, so a Slack toggle and a
    typed 'brb'/'I'm back' agree, and refresh the pinned Slack control message.

    ON transition: posts an announcement ping (with an 'I'm back' button) AND
    sets server AFK on. This also closes the mid-turn gap - typing 'afk' while
    Claude is working queues the prompt until after the current Stop has already
    read the marker as absent, so without this immediate push Slack would stay
    dark until the next Stop.

    OFF transition: a state-only update (no ping - turning AFK off shouldn't
    light up Slack), which just flips the server flag and updates the control
    message.

    Best-effort: any failure (network, timeout) is swallowed so a server blip
    never breaks the prompt.
    """
    if state:
        # ON: a real announcement ping on /notify (an old server posts it too,
        # which is the correct behavior for going AFK).
        url = NOTIFY_URL
        body: dict = {
            "hook_event_name": "Notification",
            "message": (
                f"AFK mode ON - matched '{trigger}'. "
                f"Slack is now the active channel. Stop events will block up to "
                f"30 min waiting for your reply."
            ),
            "set_afk": True,
            "afk_announcement": True,
        }
        cwd = payload.get("cwd")
        transcript_path = payload.get("transcript_path")
        session_id = payload.get("session_id")
        if cwd:
            body["cwd"] = str(cwd)
        if transcript_path:
            body["transcript_path"] = str(transcript_path)
        if session_id:
            body["session_id"] = str(session_id)
    else:
        # OFF: state-only sync on the dedicated /afk endpoint. Turning AFK off
        # must NOT ping Slack; routing here (not /notify) means a server that
        # predates /afk 404s silently instead of posting a spurious message.
        url = AFK_STATE_URL
        body = {"afk": False}

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=ANNOUNCE_TIMEOUT, context=_HTTP_SSL_CTX).read()
    except Exception as e:
        sys.stderr.write(f"[afk-trigger] state push failed ({e}); will rely on next Stop\n")
        sys.stderr.flush()


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

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        sys.exit(0)

    # Latest-position-wins when both OFF and ON triggers appear in the same
    # prompt. "I'm back, but going to bed" should land on ON (going to bed
    # is the later, controlling intent). "back now, give me an update on
    # the brb branch" lands on OFF (back is the later intent vs. the
    # parenthetical "brb branch" reference). Falls back to a one-sided
    # match when only one kind of trigger is present.
    off_match = _earliest_match(_AFK_OFF_PATTERNS, prompt)
    on_match = _earliest_non_negated_on_match(_AFK_ON_PATTERNS, prompt)
    # Gate ON to short, cue-dominant messages: a trigger word buried in a longer
    # sentence is discussion, not a departure, and a false ON pings Slack at the
    # desk. Explicit `afk.sh on` / the Slack toggle bypass this entirely. OFF is
    # never gated (turning off by mistake is the safe direction).
    if on_match and not _is_departure_message(prompt):
        on_match = None

    if off_match and on_match:
        # Whichever appears LATER in the prompt wins.
        if on_match[0] > off_match[0]:
            chosen_state, chosen_trigger = True, on_match[1]
        else:
            chosen_state, chosen_trigger = False, off_match[1]
    elif off_match:
        chosen_state, chosen_trigger = False, off_match[1]
    elif on_match:
        chosen_state, chosen_trigger = True, on_match[1]
    else:
        sys.exit(0)

    changed = _set_afk(chosen_state)
    if changed:
        try:
            _emit_status("on" if chosen_state else "off", chosen_trigger)
        except Exception:
            pass
        # Sync the server on a real transition (only when the local state
        # actually changed, so repeated cues don't spam). ON announces + sets
        # the flag; OFF silently syncs the flag + control message.
        try:
            _push_afk_state(chosen_state, chosen_trigger, payload)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
