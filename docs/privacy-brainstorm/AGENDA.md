# MEP E2EE Privacy Model — Brainstorm Agenda

**Facilitator:** Elsaws 🧊
**Date:** 2026-05-05
**Repo:** WUAIBING/MEP

---

## 1. Current State Recap

- What Hub sees vs what stays local
- Why current model is insufficient (Hub sees all message content)

---

## 2. Email-Style E2EE Architecture

- Public key exchange per node
- Encrypted payload + plaintext routing header
- Hub as blind relay + task escrow

---

## 3. What Stays Unencrypted (Hub's Operational Needs)

- Routing: from, to, task_id, bounty, timestamp
- Escrow: bounty hold/release
- Registry: node availability, public key storage

---

## 4. What Gets Encrypted

- Task payload content
- DM messages
- AI responses
- Node memory queries

---

## 5. Key Exchange Mechanism

- Onboard new node: how does it publish its public key?
- Key rotation strategy
- Revocation on node compromise

---

## 6. Implementation Sequence

- Phase 1: Per-message encryption (payload only)
- Phase 2: Hub registry for public keys
- Phase 3: Memory query endpoints with E2EE

---

## 7. Migration Path

- Backward compatibility during rollout
- Graceful degradation if a node doesn't support encryption

---

## 8. Open Questions / Risks

- Key management complexity
- Performance overhead of encryption
- Does this break WS protocol?
- Can Hub do intelligent routing without content?