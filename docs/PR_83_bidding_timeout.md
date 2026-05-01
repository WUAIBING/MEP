# PR #83: Bidding Task Timeout Fix

## Problem
- 1,486+ tasks stuck in "bidding" status forever
- Tasks never get picked up by providers
- Bounty stays locked in pending (9.5+ SECONDS stuck)
- Only "assigned" tasks timeout - "bidding" never expires

## Fix
Add `get_bidding_tasks_before()` and `expire_task_if_bidding()` to db.py

## Changes
### hub/db.py
- Add `get_bidding_tasks_before(cutoff_ts)` - query all bidding tasks older than cutoff
- Add `expire_task_if_bidding(task_id)` - mark bidding task as expired

### hub/main.py
- Add `_sweep_bidding_timeouts()` - similar to assigned sweep but for bidding
- Add to `_assignment_timeout_worker()` to run every sweep cycle

## Testing
After restart, check:
- `SELECT COUNT(*) FROM tasks WHERE status='bidding'` - should decrease over time
- Balance sheet - locked bounty should release after timeout

## Note
Uses same timeout as assigned (1 hour by default). Future PR will add consumer-configurable timeout.