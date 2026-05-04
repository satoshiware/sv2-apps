Fix List

Add epoch persistence to freeze work basis per matured window.
Add pending-block ledger so each blockhash carries its original epoch and payout state.
Make blocked-window accrual idempotent so the same window cannot accrue multiple times.
Resolve and pay rewards by epoch group, not by a single current window basis.
Track blocked attempts in audit logs with full context and retry diagnostics.
Keep retry selection bounded and deterministic to avoid accidental broad reprocessing.
Add migration-safe rollout guards and a feature flag for the new epoch allocator.
Expand integration tests for mixed-resolution, repeated blocked cycles, and idempotency.
Code Areas To Change

Models and schema:
models.py
Settlement orchestration and blocked/retry flow:
main.py
Allocation engine and accrual behavior:
settlement.py
Audit payload and blocked-attempt logging:
audit.py
Tests:
test_health.py
test_settlement.py
Detailed Items

Create table work_epoch with unique key on window_start + window_end and status lifecycle open, partially_paid, paid.
Create table work_epoch_user_basis with frozen share_delta and work_delta per user per epoch.
Create table pending_block_reward with blockhash unique, epoch_id, reward_sats nullable, resolved_at, paid boolean, paid_settlement_id nullable.
In blocked branch, persist or reuse epoch and insert pending rows for current matured hashes, then accrue once per epoch.
Add accrued_applied or equivalent marker on epoch to prevent double accrual when blocked repeats.
On each cycle, resolve rewards for unresolved pending rows and group by epoch_id.
Allocate each resolved epoch reward against that epoch frozen basis only, then mark pending rows paid.
Link payout outputs to settlement and epoch for traceability.
Keep current settlement window payout independent from old epoch payouts in the same cycle.
Write blocked audit entry before early return, including missing_current_matured_hash_count and unresolved hashes.
Add tests:
same blocked window retried multiple times does not multiply accrued work.
old epoch reward resolves later and pays with old basis.
current epoch reward in same cycle pays with current basis, not mixed.
repeated API retries are idempotent, no duplicate payouts.
If you want, I can turn this list into an implementation sequence of small PR-sized steps next.