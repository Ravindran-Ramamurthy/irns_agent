
irns agent is based on document at /home/ravi/Downloads/irns_srs.docx. create an md file with seperate section defining in seperate sections for each risk event type. list steps to be taken in sequence from when the message arrives and time gap when the steps results need to be reviewed (10 or 15 minutes...) and handlign when user events (input event is received) as two sub sections for every risk event type

# IRNS Risk Event Workflows

Source: `IRNS_SRS.docx` ("Sample SRS for workflow Implementation", Doc No. D2320010, v1.0, 2-Apr-26).

This document breaks the SRS down by **risk event type** (alert code). Each section lists the
sequential steps IRNS takes from the moment the risk-monitoring alert arrives, the wait/review
interval before each escalation, and how a customer response (an *incoming event*, arriving via
IVR/SMS/Email) is handled if it lands mid-sequence.

Terms, mapped to the SRS and to the agent's own vocabulary (`app/state.py`):

| SRS term | Meaning | Agent state `source` |
|---|---|---|
| Alert / risk event | Notification from the risk monitoring system that starts a case | `riskevent` |
| Customer response | Option 1 (Accept), Option 6 (Dispute), or silence, via IVR/SMS/Email | `incomingevent` |
| IRNS action | Block/unblock IB, send SMS/Email, make IVR call, queue the case | `actionevent` |

## 1. Data Needed for Every Workflow

Fetched per alert, from different upstream systems, keyed off the alert/action code:

- Alert Code
- Customer's current Email id and mobile number
- Customer's Email id and mobile number *prior to* the change request
- Customer Id
- Transaction date and time
- Customer name
- Customer's old mobile number / old Email id
- Customer's preferred IVR language (English/Hindi — default English)

## 2. Rules Common to All Workflows

These apply inside every risk event type's "Handling of Input Events" subsection unless a
section overrides them:

- **Option 1 — Accept the transaction** (any channel: IVR/SMS/Email): unblock the IB, close the
  status in the Alert system with appropriate comments.
- **Option 6 — Dispute the transaction**: block the IB (by customer id), move the case to the
  `IRNS_Dispute_Case` queue, send a "Dispute Case" message to the Alert system, and close the
  case in the Alert system.
- **No response** to any of the IVR calls / SMS / Email in the workflow: move the case to the
  Agent queue (`IRNS Dispute`) for manual handling, and close the case in IRNS.
- **Every block/unblock of the IB** triggers an SMS + Email to the customer confirming the new
  state, plus a corresponding comment to the Alert system.
- **If IRNS cannot block/unblock** the IB (EOD in progress, service unavailable, etc.): move the
  case to `COS_IRNS_IBBLOCK_QUEUE` for an analyst to handle manually, and notify the Alert system.

> ⚠️ Source-document ambiguity: for workflows 100011, 100021 and 100041, the SRS states *both* —
> "if no response, move to `IRNS_SPK_Analyst` queue" and "if no response, move to
> `IRNS_Dispute_Case` queue" — as two separate bullets under the same trigger condition. It is not
> specified whether both queues are used (e.g. `IRNS_SPK_Analyst` as first-level review and
> `IRNS_Dispute_Case` as a fallback) or whether this is a copy/paste duplication in the SRS. Flag
> for BA confirmation before implementing the no-response branch for those three workflows.

---

## 3. Risk Event Type: Change in Mobile No — Alert Code 100011 (`ChMobile`)

Channel: **Email only** — no IVR calls, no SMS.

### 3.1 Sequential Steps & Review Timers

| # | Trigger | Action | Wait before next check |
|---|---|---|---|
| 1 | Alert received | Send **EMAIL1** — transaction alert notification (1st time) | 15 min |
| 2 | No response after EMAIL1 | Block the IB (linked customer id); send **EMAIL2** — IB blocked notification | 15 min |
| 3 | No response after EMAIL2 | Send **EMAIL3** — transaction alert notification (reminder) | 15 min |
| 4 | No response after EMAIL3 | Send **EMAIL4** — transaction alert notification (reminder) | 15 min* |
| 5 | Still no response after EMAIL4 | Escalate — see 3.2 | — |

\* The SRS specifies 15-minute waits between EMAIL1→EMAIL2→EMAIL3→EMAIL4 explicitly, but does
not state an explicit wait after EMAIL4 before escalating. Assumed consistent at 15 min per the
established pattern — confirm with BA.

EMAIL1–EMAIL5 templates share fixed content plus variable content (last 4 digits of
account/card number, amount, etc.).

### 3.2 Handling of Input Events

- **Accept, at any point in the sequence**: unblock the IB (if it was already blocked by step 2)
  and send **EMAIL5** — IB unblocked notification. Close the case per the common rules (§2).
- **Dispute, at any point in the sequence**: apply the common dispute rule (§2) — block IB, move
  to `IRNS_Dispute_Case`, notify Alert system, close case.
- **No response through EMAIL4**: move the case to `IRNS_SPK_Analyst` queue **and/or**
  `IRNS_Dispute_Case` queue (see ambiguity note in §2).

---

## 4. Risk Event Type: Change in Email — Alert Code 100021 (`ChEmail`)

Channel: up to **12 IVR calls** and **4 SMS** — no email in the base flow (see EMAIL5 note below).

### 4.1 Sequential Steps & Review Timers

| # | Trigger | Action | Wait before next check |
|---|---|---|---|
| 1 | Alert received | Make **IVR1** (1st time) | — |
| 2 | No response to IVR1 | Send **SMS1** (transaction alert SMS); **simultaneously** block the IB for the linked customer id | — |
| 3 | Immediately after SMS1 | Send **SMS2** (IB/mobile-number-blocked SMS) | — |
| 4 | No response after close of IVR1 | Make **IVR2** | 15 sec |
| 5 | No response after close of IVR2 | Make **IVR3** | 15 sec |
| 6 | No response after close of IVR3 | Make **IVR4** | 5 min |
| 7 | No response after close of IVR4 | Make **IVR5** | 5 min |
| 8 | No response after close of IVR5 | Send **SMS3** | — |
| 9 | No response after close of IVR5 | Make **IVR6** | 5 min |
| 10 | No response after close of IVR6 | Make **IVR7** | 3 hr |
| 11 | No response after close of IVR7 | Make **IVR8** | 15 sec |
| 12 | No response after close of IVR8 | Make **IVR9** | 15 sec |
| 13 | No response after close of IVR9 | Make **IVR10** | 11 hr |
| 14 | No response after close of IVR10 | Make **IVR11** | 15 sec |
| 15 | No response after close of IVR11 | Make **IVR12** | 15 sec |
| 16 | Still no response after IVR12 | Escalate — see 4.2 | — |

> ⚠️ Step 3 label as written in the SRS is "Mobile number blocked SMS" even though this workflow
> is triggered by an **email**-change alert, not a mobile-number-change alert. Likely reused
> wording from the 100011/mobile-change context — confirm the intended SMS copy with BA before
> wiring the template.

### 4.2 Handling of Input Events

- **Accept, at any point**: unblock the IB and send an unblock notification.
  > ⚠️ The SRS literally says "send IB unblocked notification **EMAIL5**" here, though this
  > workflow's channel is IVR/SMS, not email. Likely should be an SMS (or SMS+Email) — confirm
  > wording/channel with BA; do not assume an email step exists in this flow without it.
- **Dispute, at any point**: apply the common dispute rule (§2).
- **No response through IVR12**: move the case to `IRNS_SPK_Analyst` queue **and/or**
  `IRNS_Dispute_Case` queue (see ambiguity note in §2).

---

## 5. Risk Event Type: Change in Mobile No & Email Id — Alert Code 100031 (`ChMobEmail`)

No wait/review timers in this workflow — everything fires immediately and the case is hand off
to a live agent.

### 5.1 Sequential Steps & Review Timers

| # | Trigger | Action | Wait before next check |
|---|---|---|---|
| 1 | Alert received | Block the customer's internet banking (IB) immediately | — |
| 2 | Immediately after block | Send IB-blocked SMS to **both** the old and the new mobile number | — |
| 3 | Immediately after block | Send IB-blocked Email to **both** the old and the new email id | — |
| 4 | Immediately after block | Transfer the call to the `IRNS_IBBlock` queue for an agent to handle | — |

There is no automated "wait N minutes and re-check" step — the case is with a live agent as soon
as the alert is processed.

### 5.2 Handling of Input Events

- Once the case is in `IRNS_IBBlock`, further customer interaction (accept/dispute) is handled by
  the live agent, not by an automated IRNS timer/response loop.
- If the agent subsequently records an accept/dispute outcome, apply the common rules in §2 for
  unblocking/blocking and closing the case in the Alert system.
- If IRNS itself fails to perform the initial block (step 1) due to EOD/service unavailability,
  apply the common failure rule in §2 (`COS_IRNS_IBBLOCK_QUEUE`) instead of routing to
  `IRNS_IBBlock`.

---

## 6. Risk Event Type: Low Risk Transaction — Alert Code 100041 (`LowRiskTr`)

Channel: up to **12 IVR calls**, **4 SMS**, and **4 Email** — same cadence as §4 (Change in
Email, 100021), with an Email sent alongside every SMS. Content of the IVR/SMS/Email differs
from the 100021 case.

### 6.1 Sequential Steps & Review Timers

| # | Trigger | Action | Wait before next check |
|---|---|---|---|
| 1 | Alert received | Make **IVR1** (1st time) | — |
| 2 | No response to IVR1 | Send **SMS1** + **Email1** (transaction alert); **simultaneously** block the IB | — |
| 3 | Immediately after SMS1/Email1 | Send **SMS2** + **Email2** (IB blocked) | — |
| 4 | No response after close of IVR1 | Make **IVR2** | 15 sec |
| 5 | No response after close of IVR2 | Make **IVR3** | 15 sec |
| 6 | No response after close of IVR3 | Make **IVR4** | 5 min |
| 7 | No response after close of IVR4 | Make **IVR5** | 5 min |
| 8 | No response after close of IVR5 | Send **SMS3** + **Email3** | — |
| 9 | No response after close of IVR5 | Make **IVR6** | 5 min |
| 10 | No response after close of IVR6 | Make **IVR7** | 3 hr |
| 11 | No response after close of IVR7 | Make **IVR8** | 15 sec |
| 12 | No response after close of IVR8 | Make **IVR9** | 15 sec |
| 13 | No response after close of IVR9 | Make **IVR10** | 11 hr |
| 14 | No response after close of IVR10 | Make **IVR11** | 15 sec |
| 15 | No response after close of IVR11 | Make **IVR12** | 15 sec |
| 16 | Still no response after IVR12 | Escalate — see 6.2 | — |

> ⚠️ §6.0 of the SRS states a maximum of "4 SMS and 4 Emails," but the step-by-step text (reused
> from §4.0) only enumerates 3 explicitly (transaction alert, IB-blocked, and the mid-sequence
> reminder). The 4th SMS/Email pair is inferred here as the **unblock notification** sent on
> acceptance (see 6.2) to reconcile the stated maximum — this is a derived assumption, not an
> explicit SRS step; confirm numbering with BA.

### 6.2 Handling of Input Events

- **Accept, at any point**: unblock the IB and send **SMS4 + Email4** (IB unblocked notification)
  — see numbering caveat above.
- **Dispute, at any point**: apply the common dispute rule (§2).
- **No response through IVR12**: apply the common no-response rule (§2) — move to Agent queue
  (`IRNS Dispute`), close the case in IRNS.

---

## 7. Risk Event Type: No Block Transaction — Alert Code 100051 (`NoBlTr`)

Channel: up to **12 IVR calls**, **3 SMS**, and **3 Email** — same base cadence as §6 (Low Risk
Transaction, 100041), minus IB blocking/unblocking entirely (the IB is never blocked for this
alert type, regardless of customer response).

### 7.1 Sequential Steps & Review Timers

| # | Trigger | Action | Wait before next check |
|---|---|---|---|
| 1 | Alert received | Make **IVR1** (1st time) | — |
| 2 | No response to IVR1 | Send **SMS1** + **Email1** (transaction alert) — **no IB block** | — |
| 3 | No response after close of IVR1 | Make **IVR2** | 15 sec |
| 4 | No response after close of IVR2 | Make **IVR3** | 15 sec |
| 5 | No response after close of IVR3 | Make **IVR4** | 5 min |
| 6 | No response after close of IVR4 | Make **IVR5** | 5 min |
| 7 | No response after close of IVR5 | Send **SMS2** + **Email2** (reminder) | — |
| 8 | No response after close of IVR5 | Make **IVR6** | 5 min |
| 9 | No response after close of IVR6 | Make **IVR7** | 3 hr |
| 10 | No response after close of IVR7 | Make **IVR8** | 15 sec |
| 11 | No response after close of IVR8 | Make **IVR9** | 15 sec |
| 12 | No response after close of IVR9 | Make **IVR10** | 11 hr |
| 13 | No response after close of IVR10 | Make **IVR11** | 15 sec |
| 14 | No response after close of IVR11 | Make **IVR12** | 15 sec |
| 15 | Still no response after IVR12 | Escalate — see 7.2 | — |

Changes applied relative to §6 (Low Risk Transaction), per §7.0 of the SRS:

- (a) No IB-blocking step, and no corresponding "IB blocked" SMS/Email.
- (b) No IB-unblocking step on acceptance.

> ⚠️ The table above accounts for only 2 SMS/2 Email explicitly (transaction alert, reminder),
> while §7.0 states a maximum of "3 SMS and 3 Emails." The source text does not spell out the
> content or trigger point of the 3rd SMS/Email once blocking is removed from the flow —
> confirm with BA before implementation (possible candidates: a closing confirmation SMS/Email
> on acceptance, or one more reminder later in the IVR sequence).

### 7.2 Handling of Input Events

- **Accept, at any point**: since the IB is never blocked, there is nothing to unblock — close
  the case per the common rules (§2). Confirm with BA whether a confirmation SMS/Email is still
  sent on acceptance (see numbering caveat above).
- **Dispute, at any point**: apply the common dispute rule (§2) — this is the one path where the
  IB does get blocked, per the block-on-dispute rule in §2.
- **No response through IVR12**: apply the common no-response rule (§2) — move to Agent queue
  (`IRNS Dispute`), close the case in IRNS.
