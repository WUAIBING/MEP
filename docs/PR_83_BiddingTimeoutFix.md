# PR #83: Bidding Task Timeout Fix

## Problem
- 1,486+ tasks stuck in "bidding" status forever
- Only "assigned" tasks timeout - "bidding" never expires
- Bounty (~9.5 SECONDS) stays locked

## Solution
Add bidding task sweep to expire stuck tasks after timeout.

## Changes

### hub/db.py
- Add `get_bidding_tasks_before(cutoff_ts)` - query all bidding tasks older than cutoff
- Add `expire_task_if_bidding(task_id)` - mark bidding task as expired

### hub/main.py  
- Add `_sweep_bidding_timeouts()` - sweep stuck bidding tasks
- Add to `_assignment_timeout_worker()` to run every cycle

## Testing
After restart:
- Check `SELECT COUNT(*) FROM tasks WHERE status='bidding'` - should decrease
- Balance sheet - locked bounty should release after timeout

## Note
Uses same timeout as assigned (1 hour default). Future PR #84 adds consumer-configurable timeout.
