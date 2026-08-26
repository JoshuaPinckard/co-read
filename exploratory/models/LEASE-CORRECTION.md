# Lease rule correction

The frozen instrument's L* of 26 seconds (HAZARD.md section 3) was an
artifact of its cost model, which charged one agent-minute of blocking per
dangling minute and therefore assumed a second agent is always waiting for
the region. Measured read multiplicity says contention is the exception:
p50 = 1 and p90 = 1 actor per file per 24 hours over n=1,667 file-day cells,
with p99 = 4, bracketing the two-actor file-day fraction between 1% and 10%.
Per-dangling-episode demand is lower still, so the bracket is an upper bound.

## Corrected objective and result

cost(L, p) = P(T > L) * C_reacquire + E[min(L, D)] * p * C_block, with p the
probability a second actor wants the region during the dangling window and
the same quantile reconstructions, C_reacquire = 3.3275 agent-minutes, and
C_block = 1 agent-minute per waiting minute. Instrument:
[lease_correction.py](../../instruments/models/lease_correction.py), output
[lease_correction.json](lease_correction.json).

| p (contention) | L* | False expiry at L* |
|---|---|---|
| 0.005 | 60 min | 10.0% |
| 0.01 | 58 min | 10.5% |
| 0.02 | 58 min | 10.5% |
| 0.03 | 0.5 min | 50.0% |
| 0.10 | 0.5 min | 50.0% |

The objective is bimodal with breakeven p* = 0.023. Below it the optimum
sits at 58 to 60 minutes, pinned by the read-to-write p90 of 58.66 minutes.
Above it, short leases win because re-acquisition is cheaper than making a
waiting agent idle. The measured bracket [0.01, 0.10] straddles p*, so **no
single constant is correct for all keys**.

## The rule that ships

Per-key adaptive lease, computed from the system's own index rather than
chosen: a region whose observed live multiplicity is 1 holds a **60-minute
renewable lease** (the low-contention optimum, and the value the eyeballed
seed happened to land on). When the index observes a second live reader or
an arriving claim on the region, the lease demotes toward the short regime
(order one minute) for that key. The index already measures per-key
multiplicity, so p is observed per key, not assumed globally. Tag:
`@perrepo(read-to-write p90; per-key observed contention vs p* = 0.023)`.

## Honest limits

T and D are piecewise-linear reconstructions from retained percentile
summaries, not raw samples, matching the frozen instrument. C_reacquire is
a workload proxy (p50 active span), not a measured relaunch benchmark. D is
right-censored at observed end, so E[min(L, D)] at large L overstates
dangling and biases against long leases, meaning the 60-minute regime is
conservative. The file-day contention bracket is an upper bound on
per-episode contention, which favors the long-lease regime further.
