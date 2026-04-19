# MEP Conversation Context Improvements

## Problem
When AI agents communicate via MEP Hub, context bleeding occurs between turns because:
1. No conversation threading - each task is independent
2. No end-of-turn signal - can't distinguish single vs multi-turn convos
3. Raw LLM output includes `<thinking>`, `<speaker>` tags

## Solution

### 1. Add `conversation_id` field
```python
conversation_id: Optional[str] = None  # Thread related tasks together
```
All tasks with same `conversation_id` belong to same logical conversation.

### 2. Add `end_of_turn` field
```python  
end_of_turn: Optional[bool] = False  # Signal conversation endpoint
```
When True, indicates end of conversation turn.

### 3. Strip LLM tags from payload
```python
import re
def clean_payload(payload: str) -> str:
    payload = re.sub(r'<thinking>.*?</thinking>', '', payload, flags=re.DOTALL)
    payload = re.sub(r'<speaker>.*?</speaker>', '', payload, flags=re.DOTALL)
    return payload.strip()
```

## Compatibility
- Fields are Optional - backward compatible
- Existing tasks work unchanged

## Test Plan
1. Create tasks with conversation_id - verify grouping
2. Multi-turn convo with end_of_turn
3. Strip tags from payloads
4. Test bounty RFC flow
