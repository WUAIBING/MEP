# PR #84: Consumer-Controlled Task Expiration

## Problem
- Fixed 1-hour timeout doesn't work for all task types
- Consumer should control how long to wait for a provider

## Solution
Add optional `task_expires_after` field to task submission.

## Changes
### hub/models.py
```python
class TaskCreate(BaseModel):
    # ... existing fields ...
    task_expires_after: Optional[int] = Field(
        default=3600,  # 1 hour default
        description="Task expires after this many seconds if not assigned"
    )
```

### hub/main.py
- In submit: store `expires_at = created_at + task_expires_after`
- In sweep: check `expires_at` not just fixed timeout

### hub/db.py
- Add `expires_at` column to tasks table (optional)
- Query by expiration time

## Usage
```json
{
  "consumer_id": "...",
  "payload": "do something",
  "bounty": 0.001,
  "task_expires_after": 1800  // Consumer sets 30 min timeout
}
```

## Benefits
- Quick tasks: 15-30 min timeout
- Normal: 1-2 hours  
- Patient: hours/days
- Consumer controls risk tolerance
