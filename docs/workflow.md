# ROVR Workflow

## Overview

Each scene passes through a three-stage review before it is marked approved (status 7):

1. **Analyst 1** claims the scene and draws ROIs in ROI Studio
2. **Analyst 2** (a peer) claims the submitted scene and reviews the ROIs
3. **Supervisor** claims and gives final approval

At any stage a reviewer can kick the scene back to Analyst 1, optionally with a comment. Once kicked back and fixed, **resubmissions bypass peer review and go directly to the supervisor**.

---

## Terminology

**Scene**: the unit of work in ROVR. One set of ROIs drawn on a planetary image, tracked from initial claim through final approval. Corresponds to one `.sel` file on `R:\Rice\Pancam`.

**Analyst 1 (Owner)**: the analyst who originally claimed the scene and drew its ROIs. Recorded as `owner_id`; this never changes. Responsible for all resubmissions regardless of who kicked the scene back.

**Analyst 2 (Peer Reviewer)**: the analyst who claims the scene for peer review (status 2 to 3). Cannot be Analyst 1. Recorded as `peer_reviewer_id` when they submit their decision.

**Supervisor**: claims and reviews scenes that have passed peer review (status 5 to 6). Any active supervisor can claim any status-5 scene. Recorded as `supervisor_id` when they submit their decision.

**Scene Pool (status 0)**: the shared list of unclaimed scenes. Any analyst can claim from it.

**Peer Review Pool (status 2)**: the shared list of submitted scenes available for peer review. The owning analyst cannot claim their own scene from it.

**Supervisor Pool (status 5)**: the shared list of scenes awaiting supervisor claim. Any supervisor can claim from it.

---

## Scene Lifecycle

```
stateDiagram
    direction LR

    [*] --> s0

    s0: 0 · unclaimed
    s1: 1 · claimed
    s2: 2 · pending review
    s3: 3 · in review
    s4: 4 · needs revision
    s5: 5 · pending supervisor
    s6: 6 · in supervisor review
    s7: 7 · approved
    s8: 8 · issues

    s0 --> s1 : analyst 1 claims
    s1 --> s2 : analyst 1 submits

    s2 --> s3 : analyst 2 claims
    s3 --> s5 : analyst 2 approves
    s3 --> s4 : analyst 2 kicks back

    s4 --> s5 : analyst 1 resubmits, no supervisor assigned yet
    (bypasses peer review)
    s4 --> s6 : analyst 1 resubmits, supervisor already assigned
    (bypasses peer review, returns straight to that supervisor's queue)

    s5 --> s6 : supervisor claims
    s6 --> s4 : supervisor kicks back
    s6 --> s7 : supervisor approves
    s6 --> s8 : supervisor marks bad scene

    s7 --> [*]
    s8 --> [*]

    s1 --> s0 : owner deactivated (ownership cleared)
    s3 --> s2 : owner OR peer reviewer deactivated (claim cleared)
    s4 --> s0 : owner deactivated (ownership cleared)
    s6 --> s5 : supervisor deactivated (claim cleared)
```

`s4 → s5` vs `s4 → s6`: a resubmission's destination depends on whether `supervisor_id` is set. A peer-reviewer-only kickback (`s3 → s4`) leaves it unset, so resubmitting goes to the general pool (`s5`). A supervisor kickback (`s6 → s4`) sets it, so resubmitting returns straight to that supervisor's queue (`s6`), since they asked for the fix and are the right person to verify it.

`s8` (issues) is reached from `s6` via **Mark Bad Scene** in My Work Queue, the shortcut for a scene flagged earlier in its lifecycle reaching supervisor review. Flagging never changes a scene's workflow path; it is a marker any user can set at any stage. `s8` is also reachable from any status via Edit Scene in the master list.

---

## Step-by-Step

### Stage 0: Scene Pool (status 0, unclaimed)

Scenes are pre-loaded into the database with status `0` and appear in the **Scene Pool** visible to all analysts.

### Stage 1: Analyst 1 claims and draws (status 1, claimed)

1. Analyst 1 opens ROVR and sees the Scene Pool.
2. Analyst 1 claims a scene, status becomes `1`. It moves off the shared pool into Analyst 1's personal to-do list.
3. Analyst 1 opens the scene in ROI Studio and draws ROIs.
4. Analyst 1 submits the scene in ROVR, status becomes `2`.

### Stage 2: Analyst 2 peer-reviews (status 2 to 3)

1. Analyst 2 sees the scene in the **Peer Review Pool** (status-2 scenes they don't own).
2. Analyst 2 claims it, status becomes `3`. The scene is no longer visible to other analysts.
3. Analyst 2 opens the scene in ROI Studio and inspects the ROIs.
4. Analyst 2 submits a decision:
   - **Approve**: `peer_reviewer_id` set, status becomes `5`
   - **Kick back** (comment optional but recommended): `peer_reviewer_id` set, status becomes `4`

### Stage 3: Analyst 1 revises (status 4, needs revision)

1. Analyst 1 sees the scene in their to-do list with the reviewer's comments, if any.
2. Analyst 1 makes the requested edits in ROI Studio.
3. Analyst 1 resubmits, **skipping peer review**:
   - If `supervisor_id` is set (the kickback came from a supervisor), status becomes `6`, back in that supervisor's to-do list.
   - Otherwise, status becomes `5`, the general Supervisor Pool.

### Stage 4: Supervisor claims and approves (status 5 to 6)

A scene reaches a supervisor's to-do list either by being claimed from the shared Supervisor Pool, or automatically via a resubmission already associated with them (Stage 3, step 3).

By the time a scene arrives here its **summary slide** has already been built: a one-page PDF with the left-eye DCS, the right-eye RGB, the spectra plot and a table of the analyst's per-ROI metadata. It is generated when the scene becomes supervisor-bound, so the wait falls on the analyst rather than the supervisor. The **Summary Slide** button, on the tray and inside the Review dialog, opens it in the system PDF viewer, rebuilding first only if an analyst re-saved since.

1. Supervisor sees the scene in the **Supervisor Pool**.
2. Supervisor claims it, status becomes `6`.
3. Supervisor reviews the scene, usually starting from the summary slide.
4. Supervisor submits a decision:
   - **Approve**: `supervisor_id` set, status becomes `7`. Scene is complete.
   - **Kick back** (comment optional but recommended): `supervisor_id` set, status becomes `4`, returns to Analyst 1.
   - **Mark Bad Scene**: `supervisor_id` set, status becomes `8`. The scene leaves the normal workflow; the audit log records which flags were set on it.

---

## Status Reference

| Status | Name | Queue | Who acts next |
|--------|------|-------|---------------|
| `0` | unclaimed | Scene Pool, shared, all analysts | Any analyst |
| `1` | claimed | Analyst 1's to-do list | Analyst 1 (owner) |
| `2` | pending review | Peer Review Pool, shared, all analysts except owner | Any analyst except owner |
| `3` | in review | Analyst 2's to-do list | Analyst 2 (peer reviewer) |
| `4` | needs revision | Analyst 1's to-do list | Analyst 1 (owner) |
| `5` | pending supervisor | Supervisor Pool, shared, all supervisors | Any supervisor |
| `6` | in supervisor review | Supervisor's to-do list | Supervisor (claimant) |
| `7` | approved | terminal | none |
| `8` | issues | terminal | none |

**Shared pools** (scenes visible to all users of a role): status 0, 2, and 5.

---

## Role Capabilities

### Analyst

| Action | Required condition |
|--------|--------------------|
| Claim a scene from the pool | Scene is status 0; analyst is not the owner |
| Submit a scene | Scene is status 1 or 4; analyst is the owner |
| Claim a scene for peer review | Scene is status 2; analyst is not the owner |
| Approve or kick back a claimed scene | Scene is status 3; analyst is the claimant |
| Release a claimed scene back to the pool | Scene is status 1 or 3; analyst is the claimant |

### Supervisor

| Action | Required condition |
|--------|--------------------|
| Claim a scene from the Supervisor Pool | Scene is status 5 |
| Approve, kick back, or mark bad a claimed scene | Scene is status 6; supervisor is the claimant |
| Force-release a stuck claim | Scene is status 3 |

---

## User Deactivation

When a user is deactivated via `manage_users.py deactivate <username>`, their open scenes are returned to the shared pools in the same transaction:

- **Status 1 or 4** (owner's in-progress work): status `0`, ownership cleared so a new analyst can claim fresh
- **Status 2** (peer review pool, unclaimed): status unchanged, ownership cleared. Still claimable by anyone, now ownerless.
- **Status 3**, deactivated user is the **owner**: status `2`, claim and ownership cleared so a different analyst can still review the existing ROI work
- **Status 3**, deactivated user is the **peer reviewer**: status `2`, claim released so another analyst can pick up the review; ownership untouched
- **Status 5** (supervisor pool, unclaimed): status unchanged, ownership cleared. Still claimable by any supervisor, now ownerless.
- **Status 6**, deactivated user is the **owner**: status unchanged, ownership cleared. The supervisor's claim is untouched and can still be approved with no owner.
- **Status 6**, deactivated user is the **supervisor**: status `5`, claim cleared so another supervisor can claim it; ownership untouched

An ownerless scene that is later **kicked back** has no one left to revise it, so it goes to status `0` instead of `4`. Whoever claims it next becomes its new owner.
