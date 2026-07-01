# controller.py
"""controller logic"""
from app.db import (
    get_scene_by_id, update_scene_status, log_review,
    claim_from_pool as db_claim_from_pool,
    claim_for_review as db_claim_for_review,
    claim_for_supervisor_review as db_claim_for_supervisor_review,
    release_scene, release_supervisor_review as db_release_supervisor_review,
    set_peer_reviewer, set_supervisor, get_user_by_id, reset_scene,
    update_scene_assignments,
)
from app.models import SceneStatus, Decision, Stage, SceneFlag


def claim_from_pool(conn, scene_id, analyst_id):
    """analyst 1 claims an unclaimed scene from the pool (0 → 1)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene is None:
        raise ValueError(f"Scene {scene_id} not found")
    if scene['status'] != SceneStatus.UNCLAIMED:
        return False
    return db_claim_from_pool(conn, scene_id, analyst_id)


def submit_scene(conn, scene_id, analyst_id):
    """analyst 1 submits a scene — claimed (1) goes to peer review (2),
    needs revision (4) goes directly to supervisor (5), bypassing peer review"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] not in (SceneStatus.CLAIMED, SceneStatus.NEEDS_REVISION):
        raise ValueError("Only claimed or needs-revision scenes can be submitted")
    if scene['owner_id'] != analyst_id:
        raise ValueError("Only the scene owner can submit")
    if scene['status'] == SceneStatus.CLAIMED:
        update_scene_status(conn, scene_id, SceneStatus.PENDING_REVIEW)
        log_review(conn, scene_id, analyst_id, Stage.SUBMISSION, Decision.SUBMITTED, None)
    else:
        update_scene_status(conn, scene_id, SceneStatus.PENDING_SUPERVISOR)
        log_review(conn, scene_id, analyst_id, Stage.RESUBMISSION, Decision.SUBMITTED, None)


def peer_review_scene(conn, scene_id, reviewer_id, decision, comments):
    """analyst 2 reviews a claimed scene (3) — approve → pending supervisor (5),
    or kick back → needs revision (4)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] != SceneStatus.IN_REVIEW:
        raise ValueError("Scene is not currently in peer review")
    if scene['owner_id'] == reviewer_id:
        raise ValueError("Analysts cannot review their own scenes")
    if decision not in Decision.VALID_REVIEW:
        raise ValueError(f"Invalid decision: {decision}")
    set_peer_reviewer(conn, scene_id, reviewer_id)
    if decision == Decision.APPROVE:
        update_scene_status(conn, scene_id, SceneStatus.PENDING_SUPERVISOR)
        log_review(conn, scene_id, reviewer_id, Stage.PEER_REVIEW, Decision.APPROVED, comments)
    else:
        update_scene_status(conn, scene_id, SceneStatus.NEEDS_REVISION)
        log_review(conn, scene_id, reviewer_id, Stage.PEER_REVIEW, Decision.NEEDS_REVISION, comments)


def mark_scene_issues(conn, scene_id, supervisor_id):
    """supervisor marks a claimed scene (6) as having issues (8) — a shortcut for
    the common case of a flagged scene reaching supervisor review, instead of
    needing to find it in the master list and use Edit Scene"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] != SceneStatus.IN_SUPERVISOR_REVIEW:
        raise ValueError("Scene is not in supervisor review")
    if scene['claimed_by'] != supervisor_id:
        raise ValueError("You can only mark scenes you have claimed")
    set_supervisor(conn, scene_id, supervisor_id)
    update_scene_status(conn, scene_id, SceneStatus.ISSUES)
    flag_ids = SceneFlag.parse(scene['flags'])
    flags_note = ', '.join(SceneFlag.LABELS[f] for f in sorted(flag_ids)) if flag_ids else 'no flags recorded'
    log_review(conn, scene_id, supervisor_id, Stage.SUPERVISOR_REVIEW, Decision.MARKED_ISSUES,
               f"Marked as issues (flags: {flags_note})")


def claim_for_supervisor_review(conn, scene_id, supervisor_id):
    """supervisor claims a scene from the supervisor pool (5 → 6)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene is None:
        raise ValueError(f"Scene {scene_id} not found")
    if scene['status'] != SceneStatus.PENDING_SUPERVISOR:
        return False
    return db_claim_for_supervisor_review(conn, scene_id, supervisor_id)


def release_supervisor_review(conn, scene_id, supervisor_id):
    """supervisor releases their claimed scene back to the supervisor pool (6 → 5)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] != SceneStatus.IN_SUPERVISOR_REVIEW:
        raise ValueError("Scene is not in supervisor review")
    if scene['claimed_by'] != supervisor_id:
        raise ValueError("You can only release scenes you claimed")
    db_release_supervisor_review(conn, scene_id)


def supervisor_review_scene(conn, scene_id, supervisor_id, decision, comments):
    """supervisor reviews a claimed scene (6) — approve → approved (7),
    or kick back → needs revision (4)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] != SceneStatus.IN_SUPERVISOR_REVIEW:
        raise ValueError("Scene is not in supervisor review")
    if scene['claimed_by'] != supervisor_id:
        raise ValueError("You can only review scenes you have claimed")
    if decision not in Decision.VALID_REVIEW:
        raise ValueError(f"Invalid decision: {decision}")
    set_supervisor(conn, scene_id, supervisor_id)
    if decision == Decision.APPROVE:
        update_scene_status(conn, scene_id, SceneStatus.APPROVED)
        log_review(conn, scene_id, supervisor_id, Stage.SUPERVISOR_REVIEW, Decision.APPROVED, comments)
    else:
        update_scene_status(conn, scene_id, SceneStatus.NEEDS_REVISION)
        log_review(conn, scene_id, supervisor_id, Stage.SUPERVISOR_REVIEW, Decision.NEEDS_REVISION, comments)


def claim_scene_for_review(conn, scene_id, analyst_id):
    """analyst 2 claims a scene from the peer review pool (2 → 3)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene is None:
        raise ValueError(f"Scene {scene_id} not found")
    if scene['owner_id'] == analyst_id:
        raise ValueError("Analysts cannot claim their own scenes for review")
    if scene['status'] != SceneStatus.PENDING_REVIEW:
        return False
    return db_claim_for_review(conn, scene_id, analyst_id)


def release_scene_to_pool(conn, scene_id, analyst_id):
    """analyst releases a claimed scene back to its pool (1 → 0, or 3 → 2)"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] not in (SceneStatus.CLAIMED, SceneStatus.IN_REVIEW):
        raise ValueError("Only claimed scenes can be released")
    if scene['claimed_by'] != analyst_id:
        raise ValueError("You can only release scenes you claimed")
    release_scene(conn, scene_id)


def force_release_scene(conn, scene_id, supervisor_id, comments=""):
    """supervisor force-releases a stuck claim back to the appropriate pool"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] == SceneStatus.IN_SUPERVISOR_REVIEW:
        db_release_supervisor_review(conn, scene_id)
    elif scene['status'] in (SceneStatus.CLAIMED, SceneStatus.IN_REVIEW):
        release_scene(conn, scene_id)
    else:
        raise ValueError("Scene does not have a releasable claim")
    log_review(conn, scene_id, supervisor_id, Stage.ADMIN, Decision.FORCE_RELEASED,
               comments or f"Claim by user {scene['claimed_by']} force-released")


def _describe_scene_edit(conn, old_scene, new_status, owner_id, peer_reviewer_id, scene_supervisor_id, claimed_by):
    """Build a human-readable summary of what an Edit Scene action changed, for the audit log."""
    def name(user_id):
        if user_id is None:
            return "(none)"
        user = get_user_by_id(conn, user_id)
        return user['username'] if user else f"user #{user_id}"

    parts = []
    if new_status != old_scene['status']:
        parts.append(f"Status: {SceneStatus.LABELS[old_scene['status']]} -> {SceneStatus.LABELS[new_status]}")
    for label, old_val, new_val in (
        ("Owner", old_scene['owner_id'], owner_id),
        ("Peer Reviewer", old_scene['peer_reviewer_id'], peer_reviewer_id),
        ("Supervisor", old_scene['supervisor_id'], scene_supervisor_id),
        ("Claimed By", old_scene['claimed_by'], claimed_by),
    ):
        if old_val != new_val:
            parts.append(f"{label}: {name(old_val)} -> {name(new_val)}")
    return "; ".join(parts) if parts else "No changes"


def supervisor_edit_scene(conn, scene_id, supervisor_id, new_status,
                           owner_id, peer_reviewer_id, scene_supervisor_id, claimed_by,
                           comments=None):
    """supervisor admin edit: set a scene's status and directly reassign owner,
    peer reviewer, supervisor, and claimed_by in one action. Pass through the
    scene's current values for any field that isn't being changed."""
    if new_status not in SceneStatus.LABELS:
        raise ValueError(f"Invalid status: {new_status}")
    old_scene = get_scene_by_id(conn, scene_id)
    if old_scene is None:
        raise ValueError(f"Scene {scene_id} not found")
    note = comments or _describe_scene_edit(
        conn, old_scene, new_status, owner_id, peer_reviewer_id, scene_supervisor_id, claimed_by
    )
    update_scene_assignments(conn, scene_id, new_status, owner_id, peer_reviewer_id, scene_supervisor_id, claimed_by)
    log_review(conn, scene_id, supervisor_id, Stage.ADMIN, Decision.SCENE_EDITED, note)


def supervisor_reset_scene(conn, scene_id, supervisor_id, comments=None):
    """supervisor fully resets a scene to unclaimed, wiping all ownership fields"""
    reset_scene(conn, scene_id)
    note = comments or "Scene reset to unclaimed by supervisor"
    log_review(conn, scene_id, supervisor_id, Stage.ADMIN, Decision.RESET, note)
