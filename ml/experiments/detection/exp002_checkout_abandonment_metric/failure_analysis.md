# exp002 failure analysis — checkout_abandonment_rate

## What shipped

New attempt-based metric `checkout_abandonment_rate` in KNOWN_METRICS:
payments created in a bucket that reach no terminal outcome within a 30-min
inactivity threshold, as a share of decidable attempts. The pass's knowledge
edge is the window end: attempts whose threshold horizon falls beyond it are
right-censored (excluded from numerator and denominator), and no event after
the window end is ever consulted — the last 30 minutes of every window
honestly carries less signal instead of reading unresolved as abandoned.

## v1 → v2: natural abandonment clumps (measured)

v1 floors (count >= 5 decidable, abs dev >= 0.10, no observed floor) produced
4 organic FPs on standard-42, all in the 03:00-11:00 UTC band: night and
morning-ramp buckets with 2-7 decidable creations where the natural 4%
abandonment strands 1-2 payments, reading as share 0.14-1.0 on tiny
denominators. Payment-level measurement of the organic cases: 11 stuck of
294 created over 6h (08-02) and 22 of 519 over 10h (08-25) — 1-2 stuck per
30-min bucket — vs the injected spike's 112 stuck of 269 over 3.5h (15-25
per bucket). v2 floors (count >= 10, abs dev >= 0.20, observed >= 0.35, plus
the global run >= 2 / volume >= 15): 0 abandonment FPs on standard-42,
quiet-42 and standard-7, and the spike fires at +1069% with MTTD 330 min.

## Remaining blind spots

- Abandonment spikes at very quiet merchants (under 10 decidable creations
  per 30-min bucket for a whole baseline+scored span) are not scored — no
  detection rather than noisy detection.
- The signal is a checkout-completion proxy: a payment resolving 31 minutes
  after creation counts as abandoned. At the 30-min threshold this matches
  the simulator (abandoned checkouts never resolve; late resolutions are
  retry attempts modeled as new payments) and Razorpay checkout-session
  expiry order of magnitude.
- Downstream gap (reported to lead, out of detection scope): the
  OpportunityBuilder only builds failed-payment retries and payment-less
  dropped orders; abandoned checkouts (a payment row stuck in `created`)
  yield NO recovery opportunities today, so this incident class surfaces
  revenue at risk without yet feeding recovery actions.
