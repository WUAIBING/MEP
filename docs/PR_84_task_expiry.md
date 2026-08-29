# PR #84: Consumer-Controlled Task Expiration

## Problem
- Fixed 1-hour timeout doesn't work for all task types
- Consumer should control how long to wait for a provider

## Solution  
Add optional `task_expires_after` field to task submission

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
- In sweep: check `expires_at` not just status

### hub/db.py
- Add `expires_at` to tasks table schema
- Add query by expiration

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
- Quick tasks: 15-30 min
- Normal: 1-2 hours  
- Patient: hours/days
- Consumer controls risk

## Note
This builds on PR #83 (bidding timeout fix)