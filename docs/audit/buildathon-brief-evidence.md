# Audit Evidence — Razorpay AI Buildathon Brief (Track 03)

Captured: 2026-09-02 by the external-research audit agent.
Method: direct `FetchURL` of official pages (primary evidence) + delegated web searches (secondary).
Labels: **VERIFIED** (fetched live 2026-09-02, quoted verbatim) / **THIRD-PARTY** (non-official source) / **NOT FOUND** / **UNCERTAIN**.

---

## 1. Official page — VERIFIED live 2026-09-02

**URL:** https://razorpay.com/buildathon/ (fetched 2026-09-02, full main-text extraction succeeded).

**Program identity (verbatim):**
- Title: "Razorpay AI Buildathon — Build. Show. Get hired."
- "Think you can build real AI? Prove it. A student-only program to discover and hire our next generation of AI Builder Interns."
- "Students only. 6 or 12 month AI Builder Internship. In-person, Bangalore, from September."
- "No resume screening. No long application. Four steps: pick a track, build something real, show your work (a public repo, a 5 minute pitch video, the architecture), and if it has signal we call you in."

**Submission requirements (verbatim, from page):** "a public repo, a 5 minute pitch video, the architecture". No deck, no live-demo requirement stated; selection = "if it has signal we call you in" → "Shortlisted builders go straight to a panel. No aptitude test. No group discussion."

**Offer (verbatim):** "₹75,000 (monthly stipend) · 6 or 12 (months, your choice) · In-person (Bangalore, from September)."

## 2. Track 03 — the assignment track — VERIFIED live 2026-09-02

Verbatim from https://razorpay.com/buildathon/ (fetched 2026-09-02):

> **03 — AI Revenue Recovery**
> Find revenue that's slipping away and win it back.
> Build an agent that detects revenue at risk, determines the right intervention, and executes a bounded recovery workflow: from payment failures and checkout abandonment to overdue receivables.
> **Why now:** Revenue loss rarely happens in one clean step. A payment degrades, a checkout gets abandoned, a subscription fails, or an invoice goes overdue. AI can now close the loop from detecting the problem to diagnosing it, choosing the right intervention, and recovering the money.
> **Example directions:** Payment degradation → root cause → recovery action, Checkout drop-off recovery, Failed-subscription recovery, B2B receivables chaser, Mandate retry sequencer, Hinglish voice recovery, Promise-to-pay tracker.
> **The bar:** Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail.

## 3. Other tracks (context) — VERIFIED live 2026-09-02

Same page, same fetch:
- **01 — AI Growth & Agentic Commerce** — bar: "Every money action explainable, bounded and gated. Show the audit trail and one failure handled gracefully."
- **02 — AI Risk Manager** — bar: "Honest metrics including false-positive cost. Strictly defense-only: anything offense-capable is disqualified."
- **04 — AI Finance Controller** — bar: "Throughput plus measured accuracy plus an honest exception list. One cherry-picked match proves nothing." (requires "a 50+ record batch of synthetic data")
- **05 — Open Track** — bar: "Show a real problem, a working product, meaningful use of AI, and evidence that it creates value."

Note: all five bars share the same judging vocabulary — bounded/gated actions, audit trails, honest measurement. Track 03's bar is the recovery-specific instance.

## 4. Judging criteria — PARTIALLY VERIFIED

- The only official judging language is the per-track "The bar" lines above. No public rubric with weights exists on the page (full extraction checked 2026-09-02).
- **THIRD-PARTY / UNVERIFIED:** evaluation axes "problem taste, build quality, AI judgment, failure recovery" appear on third-party blogs (velonx.in, careersincloud.com per `docs/research.md:154`) — not on any official source found.

## 5. Application form — VERIFIED live 2026-09-02

**URL:** https://forms.gle/d9r2gvxp8cmoZhon9 (linked from the buildathon page; fetched 2026-09-02).
- Title: "Razorpay AI Builder Internship 2026". "This form was created inside Razorpay."
- Publicly visible fields: Email, Full Name, College Name, **Graduation Year (options: 2027 / 2028 / 2029 only)**, **In-person Internship availability starting September (Yes/No)**.
- **NOT FOUND:** no deadline/close date visible on the form's public first page. (Repo's prior capture `docs/research.md:261` matches: grad years 2027–2029, September availability.)

## 6. Deadlines — UNCERTAIN (official silence persists)

- **NOT FOUND on official sources as of 2026-09-02:** the buildathon page contains no deadline; the application form's public page shows no close date.
- **THIRD-PARTY / UNVERIFIED:** "September 5, 2026" application deadline circulated on third-party blogs (velonx.in, careersincloud.com, jobseekershub.co.in — first recorded in `docs/research.md:154,262`, still not confirmed officially). Today is 2026-09-02 — if that date is real, the window closes in ~3 days.
- Supplementary deadline-verification search results: see §8 (appended after delegated search completes).

## 7. Doc-vs-reality check against repo claims

The repo's research docs (`docs/research.md:146-178,254-271`, `docs/product-strategy.md:14`) captured this brief on 2026-08-26/27. Re-verification 2026-09-02:

| Repo claim | 2026-09-02 re-check | Verdict |
|---|---|---|
| Track 03 name + requirement text verbatim (`docs/research.md:149`) | Page text identical, verbatim match | CONFIRMED |
| The bar quote (`docs/research.md:151`, `docs/product-strategy.md:14`) | Identical on live page | CONFIRMED |
| 5 tracks with the names listed (`docs/research.md:152`) | Identical (01/02/04/05 names match) | CONFIRMED |
| Offer ₹75,000/mo, 6/12 months, Bangalore from Sept (`docs/research.md:153`) | Identical | CONFIRMED |
| Submission = public repo + 5-min pitch video + architecture | Identical | CONFIRMED |
| Google form grad years 2027/2028/2029 (`docs/research.md:261`) | Form still shows exactly these | CONFIRMED |
| Sept 5, 2026 deadline = third-party only | Still absent from official page/form | CONFIRMED (still UNVERIFIED) |
| "Not listed on Devfolio/Devpost/unstop/HackerEarth" (`docs/research.md:148`) | Re-searched 2026-09-02: no listings found on any of the four platforms | CONFIRMED (negative finding) |

## 8. Delegated search addendum (web-wide hunt, accessed 2026-09-02)

Results of a dedicated web-research pass (WebSearch + FetchURL across razorpay.com, hackathon platforms, third-party blogs, social corroboration):

**Confirmed by independent re-fetch:**
- Program is self-hosted at razorpay.com/buildathon + Google Form; **NOT listed on Devfolio, Unstop, Devpost, or HackerEarth** (searched explicitly — negative finding, matching `docs/research.md:148`).
- No standalone judging section on the official page (raw-HTML grep: no matches for "judging/criteria/problem taste") — per-track "The bar" lines are the only official evaluation language.
- No deadline on the official page (raw-HTML grep: only "from September" internship start appears).

**Third-party / UNVERIFIED items (labeled, do not treat as requirements):**
- Deadline **September 5, 2026**: stated by velonx.in (2026-08-20, https://velonx.in/blog/razorpay-ai-buildathon-2026-tracks-eligibility-stipend-selection-process) and careersincloud.com (2026-08-23, https://careersincloud.com/blog/razorpay-ai-buildathon-2026-75000-stipend-internship-for-students). Counter-source: thenewviews.com (2026-08-26) says "Apply By: Not officially disclosed on the Razorpay page at the time of writing." → Still **THIRD-PARTY-ONLY as of 2026-09-02**; official silence verified twice today.
- Judging rubric "Problem Taste, Build Quality, AI Judgment, Failure Recovery": careersincloud.com + jobseekershub.co.in (both 2026-08-23), paraphrased ("Based on how Razorpay has described the process…") — **NOT verbatim-official, UNCERTAIN**.
- Form page-2 fields (Preferred duration, Resume file, Selected Track, Project Name, What the Project Solves, Public GitHub Repository URL, 5-Minute Pitch Video "can be unlisted", **"What Broke and How You Got Out"**): listed by jobseekershub.co.in (https://www.jobseekershub.co.in/2026/08/razorpay-ai-buildathon-2026-bangalore.html) — page 2 not publicly visible without advancing the form → **UNCERTAIN as verbatim form text**, but consistent with the official page's "public repo, 5 minute pitch video, the architecture" + failure-handling emphasis.
- Degree eligibility "B.Tech/M.Tech/BCA/MCA" (careersincloud) vs "any academic stream" (thenewviews) — conflicting third-party claims, **UNCERTAIN**; official page says only "Students only" + form grad years 2027/2028/2029.
- Team size: not specified anywhere; velonx.in confirms absence.

**Community corroboration of Track 03 naming/numbering:**
- YouTube participant submission titled "Razorpay Buildathon Track 03: AI Revenue Recovery" (https://www.youtube.com/watch?v=xKRYcbk7xdk).
- LinkedIn participant post "I chose Track 3: AI Revenue Recovery" (linkedin.com/posts/vaibhav-bhatt01_, activity 7498025780887687168).

**Cautions from the search pass:**
- careersincloud.com contains a likely-garbled claim about "@f5.com" contact addresses — treated as source error, not cited above.
- github.com/Razorpay-AI-Buildathon/ (3 repos) appears participant-created; **not verified as official**.
- Prior editions (2024/2025): nothing found — evidence suggests a new program launched ~August 2026 (earliest third-party coverage 2026-08-20; Instagram announcement reel 2026-08-22). **UNCERTAIN** whether earlier editions exist.
- Deadline/status deep-verify pass (Wayback, official socials, form re-check): a separate search was launched; as of the direct fetches in §§1–6 the official page and form carry no deadline and Sept 5 remains third-party-only. Any later confirmation will be recorded in §6.
