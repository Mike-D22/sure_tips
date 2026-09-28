# OddMate — Product Decisions

Date: 2026-09-27
Status: Decision record (clarifies future product direction only).

This document is the single source of truth for branding, terminology, access
tiers, pricing, conversion, and match lifecycle/results. It applies to future
documentation, future architecture planning, and future UI work.

> **Sprint 0.5 scope:** none of this is implemented now. Sprint 0.5 must **not**
> begin Flutter (client) work, pricing implementation, subscriptions, payment
> integration, result settlement implementation, or frontend design. This record
> exists so later sprints start from one agreed direction instead of from the
> prototype screenshots.

---

## 1. Brand

1. Final product brand: **OddMate**.
2. Main product/app label: **ODDMATE TIPS**.
3. Primary visual identity: the dark purple / nebula **PREPLAY TIPS** prototype
   design language, rebranded to OddMate / ODDMATE TIPS.
4. **90PLUS TIPS** prototype: use it only as a supplementary content/layout
   reference for category and detail screens. Do not use it as the product name,
   logo, primary color system, or visual identity.

### 1.1 Do not use as a final user-facing brand

- PREPLAY TIPS
- SCOREWISE
- 90PLUS TIPS
- VIP (as a standard customer-facing access term)

### 1.2 Standard customer-facing access terms

Use exactly these terms:

- Free
- Premium
- Go Premium
- Subscribe
- Select Plan
- Restore Purchases
- Manage Subscription

---

## 2. Pricing cards

Use the ScoreWise screenshot only as a layout and commercial-presentation
reference for future OddMate pricing cards. **Do not copy ScoreWise branding,
wording, exact prices, plan claims, or VIP terminology.**

A future OddMate Premium pricing card must support:

- Plan title and duration
- Popular/recommended treatment where configured
- Optional crossed-out reference/original USD display price
- Prominent actual KES selling price
- Optional savings/discount text
- Plan benefits
- Plan entitlements
- Select Plan action
- Restore Purchases
- Terms and Privacy links

Example display concept only (not a production fact):

- Original price: `USD 60.00`, crossed out
- Actual price: `KES 6,000.00`

### 2.1 Configurability requirements

- Actual prices, currencies, product IDs, billing periods, discounts, and
  entitlement sets must be configurable **server-side** and ultimately verified
  against the payment store/provider configuration.
- Do **not** hard-code the prototype or screenshot pricing values into Flutter
  business logic.

---

## 3. Free and Premium access

"Free" is a marketing and discovery term. It must **not** automatically override
the Django subscription entitlement system.

The backend is the final authority that determines whether any feature, tip,
prediction, or data item is:

- Public
- Previewable
- Free promotional content
- Premium locked content
- Available under a specific subscription entitlement

Flutter may display Free or Premium labels and route users to Go Premium, but
Flutter must **not** grant access independently. Premium content must remain
protected by server-side entitlement checks.

Future product behavior should support:

- Public/free previews where configured
- Promotional free content where configured
- Premium locked content visible with upgrade prompts where appropriate
- Go Premium as the central conversion screen for users without the required
  entitlement

---

## 4. Go Premium screen

The Go Premium screen is the central purchase/conversion experience. It should
encourage users to subscribe by clearly displaying:

- Available plans
- Pricing
- Discount/reference pricing where valid
- Benefits
- Included entitlements
- Current plan/status for subscribed users
- Select Plan actions
- Restore Purchases
- Manage Subscription
- Terms and Privacy

Do **not** create fake payment completion flows.

---

## 5. Match lifecycle and results

OddMate should use an automated match/tip lifecycle once a legitimate verified
match-results provider or reliable data source is integrated.

Desired lifecycle:

```
Tip published
    ↓
Match upcoming
    ↓
PENDING
    ↓
Match begins
    ↓
LIVE (only if supported by verified data)
    ↓
Match ends
    ↓
Backend receives/retrieves verified result
    ↓
Settlement logic evaluates the prediction
    ↓
WON, LOST, or VOID
    ↓
History, performance, and winnings update
```

Until a verified automated results source is available:

- Use `PENDING` for upcoming or unfinished matches.
- Use `UNVERIFIED` or `RESULT AWAITING CONFIRMATION` for matches that cannot yet
  be reliably settled.
- Do **not** fabricate scores.
- Do **not** fabricate `WON` or `LOST` states.
- Do **not** calculate or display fabricated winnings.
- Do **not** calculate or display "Verified Accuracy" or win-rate values from
  prototype examples.
- Do **not** claim results are verified if no verified source exists.

### 5.1 Accumulator settlement rules

These must be explicit in future settlement logic:

- Any losing leg means the accumulator is **LOST**.
- All eligible winning legs mean the accumulator is **WON**.
- A void, postponed, or abandoned leg must follow a documented settlement policy.
- Any pending or unverified leg keeps the accumulator **PENDING** or
  **UNVERIFIED**.

---

## 6. Required terminology update

Apply these in future documentation and implementation:

- Replace final product references to **PitchIQ** with **OddMate**.
- Replace final user-facing **PREPLAY TIPS** references with **ODDMATE TIPS**.
- Use **Premium** instead of **VIP** unless explicitly approved later.
- Treat prototype names, prices, performance metrics, and settled outcomes as
  visual examples only, not production facts.

---

## 7. Internal naming note

The repository/service identifiers `sure_tips` (repository) and `sure-tips-api`
(the `/api/health/` payload) are internal technical identifiers, **not**
user-facing brands. The user-facing brand is **OddMate** / **ODDMATE TIPS**.
Do not surface `sure_tips`, `sure-tips-api`, or the legacy scraper data source as
the product name.

