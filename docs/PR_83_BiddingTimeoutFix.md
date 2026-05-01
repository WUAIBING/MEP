# PR #83: Bidding Task Timeout Fix

## Problem
- 1,400+ tasks stuck in "bidding" status forever
- Only "assigned" tasks timeout - "bidding" never expires
- Bounty stays locked in escrow

## Solution
Add bidding task sweep with automatic escrow refund.

## Changes

### hub/db.py
- `get_bidding_tasks_before(cutoff_ts)` - query all bidding tasks older than cutoff
- `expire_task_if_bidding(task_id)` - mark bidding task as expired

### hub/main.py  
- `_sweep_bidding_timeouts()` - sweep and refund stuck bidding tasks
- Runs in `_assignment_timeout_worker()` every 60s
- Uses same 1-hour timeout as assigned tasks

## Refund Logic
```
1. Find bidding tasks older than 1 hour
2. Mark each as "expired" in DB
3. Call refund_escrow(task_id) - returns bounty to consumer
4. If no escrow found, add balance directly
5. Remove from in-memory active_tasks
6. Log audit trail: BIDDING_TIMEOUT_REFUND
```

## Testing (before restart)
- `SELECT COUNT(*) FROM tasks WHERE status='bidding'` → 1,486

## Testing (after restart + 1 hour)
- Check bidding count decreases
- Check balances return to consumers

## NOTE: Uses same sweeping as assigned tasks. After hub restart, tasks will start expiring after 1 hour.