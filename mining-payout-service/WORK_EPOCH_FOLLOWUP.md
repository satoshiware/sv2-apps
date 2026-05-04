# Work-Epoch Follow-Up Plan

## Goal
Implement strict attribution for late-resolving block rewards so old rewards are paid using old work basis.

## Why This Is Needed
- Current retry handling can resolve old block rewards in a newer payout cycle.
- Without epoch-linked work basis, payouts can be attributed to the wrong work window.

## Target Behavior
1. All matured blocks resolved in window:
- settle and pay using this window basis.

2. No matured blocks resolved:
- no payout for this reward path; move forward safely.

3. Some matured blocks resolved, some unresolved:
- pay resolved blocks now using their own window basis.
- keep unresolved blocks pending.
- when unresolved blocks resolve later, pay them using the original window basis.

## Proposed Data Model
1. work_epoch
- id
- window_start
- window_end
- status (open, partially_paid, paid)
- created_at
- updated_at

2. work_epoch_user_basis
- id
- epoch_id
- user_id
- share_delta
- work_delta
- created_at

3. pending_block_reward
- id
- blockhash (unique)
- found_at
- epoch_id
- reward_sats (nullable)
- resolved_at (nullable)
- paid (bool)
- paid_settlement_id (nullable)
- created_at
- updated_at

## Processing Outline
1. Build current epoch basis from matured window snapshots.
2. Insert/refresh pending_block_reward rows for current matured blocks.
3. Resolve rewards for:
- current matured blocks,
- older unpaid pending blocks.
4. Group resolved rewards by epoch.
5. For each epoch group:
- allocate payout using frozen epoch user basis,
- mark those pending rewards as paid.
6. Mark epoch paid when all its pending blocks are paid.

## Rollout Steps
1. Add new tables and migration.
2. Write epoch snapshot builder.
3. Add pending reward resolver and grouping.
4. Add epoch-based allocator path.
5. Keep current path behind feature flag during transition.
6. Add integration tests for mixed-window retries.

## Test Cases To Add
1. Window A unresolved -> Window B resolved now, Window A resolves later:
- Window A reward must pay Window A users only.

2. Partial resolution in one window:
- resolved subset paid now using same window basis,
- unresolved subset paid later using same window basis.

3. Offline miner after unresolved block:
- late reward still paid according to original epoch basis.

4. Idempotency:
- repeated cycle runs do not double-pay pending blocks.
