"""Corrected lease rule: contention probability as an explicit factor.

The frozen instrument (compute_models.py, HAZARD.md section 3) minimized
    P(T>L)*C_reacquire + E[min(L,D)]*C_block
with C_block = 1 agent-minute per waiting minute, which implicitly assumes a
second agent is ALWAYS waiting for the region. Measured read multiplicity
(PARAMETERS.md section 4: p50=1, p90=1 actor per file per 24h, p99=4 over
n=1,667 file-day cells) says contention is the exception. The corrected
objective is
    cost(L; p) = P(T>L)*C_reacquire + E[min(L,D)]*p*C_block
with p the probability a second actor wants the region during the dangling
window. p is bracketed, not point-known: p90=1 bounds the >=2-actor file-day
fraction below 10%, p99=4 bounds it above 1%, and per-dangling-episode demand
is lower still, so [0.01, 0.10] is an upper-bound bracket.

Quantile reconstructions are identical to the frozen instrument: piecewise
linear through (min 0, p50, p90, p99, max), values quoted from HAZARD.md.
"""
import json

# seconds; from HAZARD.md section 3 (n=405 read->write; n=3,094 linger)
T_Q = [(0.0, 0.0), (25.755, 0.50), (3519.388, 0.90), (133320.741, 0.99), (278233.521, 1.0)]
D_Q = [(0.0, 0.0), (83035.187, 0.50), (259336.939, 0.90), (511917.379, 0.99), (1329345.269, 1.0)]
C_REACQUIRE = 3.3275   # agent-minutes, frozen instrument's default
C_BLOCK = 1.0          # agent-minutes per waiting minute

def cdf(q, x):
    if x <= q[0][0]:
        return q[0][1]
    for (x0, p0), (x1, p1) in zip(q, q[1:]):
        if x <= x1:
            return p0 + (p1 - p0) * (x - x0) / (x1 - x0)
    return 1.0

def e_min_L_D(L):
    # E[min(L, D)] via integral of survival function of D from 0 to L
    total, step = 0.0, L / 2000.0
    if step <= 0:
        return 0.0
    x = step / 2.0
    while x < L:
        total += (1.0 - cdf(D_Q, x)) * step
        x += step
    return total

def cost(L_min, p):
    L_s = L_min * 60.0
    false_expiry = 1.0 - cdf(T_Q, L_s)
    return false_expiry * C_REACQUIRE + (e_min_L_D(L_s) / 60.0) * p * C_BLOCK

GRID = [x / 4.0 for x in range(1, 40)] + list(range(10, 241, 2)) + [300, 360, 480, 720, 1440]

def optimum(p):
    best = min(GRID, key=lambda L: cost(L, p))
    return best, cost(best, p)

rows = []
for p in [0.005, 0.01, 0.02, 0.023, 0.03, 0.05, 0.10, 0.25, 1.00]:
    L, c = optimum(p)
    rows.append({"p_contention": p, "L_star_min": L, "objective": round(c, 4),
                 "false_expiry": round(1.0 - cdf(T_Q, L * 60.0), 4)})
    print(f"p={p:<6} L*={L:>7.2f} min  objective={c:8.4f}  false_expiry={1.0-cdf(T_Q, L*60.0):.3f}")

# breakeven: largest p where the long-lease optimum still beats the short one
lo, hi = 0.001, 1.0
for _ in range(60):
    mid = (lo + hi) / 2.0
    L, _ = optimum(mid)
    if L >= 30.0:
        lo = mid
    else:
        hi = mid
print(f"breakeven p* ~= {lo:.4f} (long lease optimal below, short above)")

with open("exploratory/models/lease_correction.json", "w") as f:
    json.dump({"objective": "P(T>L)*C_reacquire + E[min(L,D)]*p*C_block",
               "C_reacquire_agent_min": C_REACQUIRE, "C_block_per_min": C_BLOCK,
               "p_bracket_measured": [0.01, 0.10],
               "rows": rows, "breakeven_p": round(lo, 4)}, f, indent=1)
print("wrote exploratory/models/lease_correction.json")
