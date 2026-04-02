# Session Handoff

**Date:** 2026-04-02
**Branch:** main

## Context
TeleClaude was migrated from an async `python-telegram-bot` base to a sync `requests`-based framework matching PolymarketAlgo's battle-tested code. PolymarketAlgo now consumes TeleClaude via editable pip install and subclasses `TeleClaudeBot`. Several post-migration bugs were fixed over multiple sessions.

## Work Completed

### v0.2.0 rewrite (commit cb0c053)
- `teleclaude/base_bot.py` — fully rewritten: async → sync raw `requests`, ported all Claude Code integration from PolymarketAlgo's 1,717-line `telegram_bot.py`
- `teleclaude/session_cli.py` — rewritten with stream-json parsing, session naming, pinned SSOT, handoff bootstrap, `on_session_fallback` callback
- `teleclaude/pyproject.toml` — dropped `python-telegram-bot>=21.0`, bumped to v0.2.0
- `teleclaude/example/main.py` — updated to sync `domain_commands()` API

### Bug fixes (commits 63f6e2a, c05cf4b)
- `base_bot.py` `get_updates`: `ConnectionResetError`/`ConnectionError` now handled silently with 2s sleep (no more `[!]` log noise from normal Telegram long-poll disconnects); other exceptions still print `[!]` and sleep 2s
- `session_cli.py`: Added debug logging for command, session start/expiry, and result
- `session_cli.py` `_parse_stream_json`: Changed `texts[-1]` → `"\n\n".join(texts)` — Claude can split a single response across many text blocks; `texts[-1]` was only the last fragment
- `session_cli.py`: `plan_max_turns` 10→25, `edit_max_turns` 15→25 — responses were truncated when Claude exhausted turns during tool-heavy analysis

### PolymarketAlgo side (separate repo, commit e093f6d)
- `telegram_bot.py`: Restored `/datastatus` command that was dropped during migration (was in old commit d210603 but not ported to new `domain_commands()`)

## In Progress
- Nothing actively in progress

## Key Decisions
- **`texts[-1]` → `"\n\n".join(texts)`**: Claude CLI's stream-json emits text incrementally across multiple blocks. A single logical response may span many blocks. Joining all of them is the correct behavior.
- **max_turns 25**: Complex analysis queries read many files; 10/15 turns was insufficient. 25 is a reasonable ceiling without being wasteful.
- **Silent ConnectionReset**: `ConnectionResetError` during long-poll is expected Telegram behavior (30s timeout), not an error. Logging it as `[!]` was noise.
- **`on_session_fallback` lambda in PolymarketAlgo**: Wired in `PolymarketTelegramBot.__init__` to send a Telegram notification when the pinned session expires.
- **Editable install**: PolymarketAlgo installs TeleClaude via `pip install -e /path/to/TeleClaude` so source changes are reflected without reinstall.

## Open Questions / Blockers
- **Multiple replies investigation incomplete**: User still reports receiving multiple replies for one Telegram message in some cases. The `_seen_update_ids` dedup set (max 200 entries) is in place. Root cause not confirmed — may be the 3-message Claude flow (status → plan → approve prompt) being misread as duplicates, or an actual dedup failure on rapid connection-reset retry cycles.

## Next Steps
1. **Confirm multiple-replies bug is resolved** after the max_turns + join-all-texts fix (truncated responses may have been causing partial sends that looked like duplicates)
2. **Push TeleClaude commits** if desired (`git push` on main)
3. **Restart PolymarketAlgo bot** after any TeleClaude changes (`pip install -e` is live, but the running process needs restart)
4. Watch for edge cases in `"\n\n".join(texts)` — if Claude's stream-json includes verbose tool output in text blocks, the joined response could be very long and hit Telegram's 4000-char limit (handled by `send_long`, but worth monitoring)

## Files Touched
- `teleclaude/base_bot.py` — sync rewrite + ConnectionReset fix + dedup set + empty-text guard
- `teleclaude/session_cli.py` — stream-json parser, session naming, pinned SSOT, max_turns increase, join-all-texts fix, debug logging
- `teleclaude/pyproject.toml` — dropped python-telegram-bot dep, v0.2.0
- `teleclaude/example/main.py` — updated to sync API
- `polymarket-algo/telegram_bot.py` (separate repo) — subclasses TeleClaudeBot, `/datastatus` restored

## Additional Context
- PolymarketAlgo `.venv`: `pip install -e "/Users/ofirc/Elementor/Claude Code/TeleClaude"` — changes to TeleClaude source take effect immediately without reinstall, but the bot process must be restarted
- TeleClaude path: `/Users/ofirc/Elementor/Claude Code/TeleClaude`
- PolymarketAlgo path: `/Users/ofirc/Elementor/Claude Code/polymarket-algo`
- The `_seen_update_ids` set in `base_bot.py` is bounded to 200 entries (discards `min` when exceeded) — this is a memory-safety measure but could theoretically drop a legitimate dedup entry under very high update volume
- `ClaudeSession.auto_pin=False` in PolymarketAlgo: session file is never auto-overwritten on resume; only written on first-ever session or explicit `pin()` call — this is intentional SSOT semantics
