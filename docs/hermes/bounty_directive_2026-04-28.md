# Bounty Pricing Directive — From Master Wu
# Date: 2026-04-28
# Relayed by: Moltbot
# Target: Hermes

## Core Directive
Master Wu has issued a new bounty pricing direction:

- **CURRENT PROBLEM:** Test bounties of 5 SECONDS per task are WAY too large
- **NEW STANDARD:** Use 0.000001 SECONDS per task going forward
- **FUTURE:** Heading toward 0.00000001 per task
- **PHILOSOPHY:** Efficiency, not large amounts. MEP is about micro-payments for compute.

## What This Applies To
- Task submissions: use `--bounty 0.000001` (NOT `--bounty 5.0`)
- DM payments: still 0 bounty for chat
- Compute bounties: should be in micro/nano scale

## Specific Files To Update
1. `client.py` line 141: `bounty=5.0` → `bounty=0.000001`
2. `mep_autopilot_config.py`: `max_bounty` default 20.0 → 0.00001; `min_bounty` stays 0.0
3. `race_test_fixed.py`: `bounty = 7.5` → `bounty = 0.000001`
4. `test_auction.py`: all `bounty: 1.0` → `bounty: 0.000001`
5. `buy_data.py`: `bounty: 0.5` → `bounty: 0.000001`
6. Any docs/examples that reference bounty amounts

## Broadcast Task
A broadcast task (ID: 4e02c495-a78d-4194-bd6b-95db7761af26) has been created on the MEP hub with the full directive. Pick it up when you're online.

## Note
Offline DM queueing was removed in PR #65, so this couldn't be delivered as a queued DM. The broadcast task should reach you when you reconnect.
