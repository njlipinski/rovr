# controller.py
"""controller logic"""
from app.db import (
    get_scene_by_id, update_scene_status, log_review,
    update_scene_assignment, claim_scene, release_scene, get_user_by_id
)

def submit_scene(conn, scene_id, analyst_id):
    """analyst 1 submits a scene for peer review"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] not in ('draft', 'needs_revision'):
        raise ValueError("Only scenes in draft or needs_revision status can be submitted")
    update_scene_status(conn, scene_id, 'pending_review')
    log_review(conn, scene_id, analyst_id, 'submission', 'submitted', 'Scene submitted for peer review')
    
def peer_review_scene(conn, scene_id, reviewer_id, decision, comments):
    """peer reviewer reviews a scene and either approves it or kicks it back to the analyst for revision"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] != 'pending_review':
        raise ValueError("Scene is not pending peer review")
    if scene['owner_id'] == reviewer_id:
        raise ValueError("Analysts cannot review their own scenes")
    if decision not in ('approve', 'request_revision'):
        raise ValueError(f"Invalid decision: {decision}")
    if decision == 'approve':
        update_scene_status(conn, scene_id, 'pending_supervisor')
        log_review(conn, scene_id, reviewer_id, 'peer_review', 'approved', comments)
    elif decision == 'request_revision':
        update_scene_status(conn, scene_id, 'needs_revision')
        log_review(conn, scene_id, reviewer_id, 'peer_review', 'needs_revision', comments)

def supervisor_review_scene(conn, scene_id, supervisor_id, decision, comments):
    """supervisor reviews a scene and either approves it or kicks it back to the analyst for revision"""
    scene = get_scene_by_id(conn, scene_id)
    if decision not in ('approve', 'request_revision'):
        raise ValueError(f"Invalid decision: {decision}")
    if scene['status'] != 'pending_supervisor':
        raise ValueError("Scene is not pending supervisor review")
    if decision == 'approve':
        update_scene_status(conn, scene_id, 'approved')
        log_review(conn, scene_id, supervisor_id, 'supervisor_review', 'approved', comments)
    elif decision == 'request_revision':
        update_scene_status(conn, scene_id, 'needs_revision')
        log_review(conn, scene_id, supervisor_id, 'supervisor_review', 'needs_revision', comments)
        
def reassign_scene(conn, scene_id, new_analyst_id, supervisor_id, comments):
    """supervisor reassigns a scene to a different analyst"""
    update_scene_status(conn, scene_id, 'needs_revision')
    update_scene_assignment(conn, scene_id, new_analyst_id)
    log_review(conn, scene_id, supervisor_id, 'reassignment', 'reassigned', comments)

def claim_scene_for_review(conn, scene_id, analyst_id):
    """analyst claims a scene for peer review — returns True if successful"""
    scene = get_scene_by_id(conn, scene_id)
    if scene is None:
        raise ValueError(f"Scene {scene_id} not found")
    if scene['owner_id'] == analyst_id:
        raise ValueError("Analysts cannot claim their own scenes")
    if scene['status'] != 'pending_review':
        return False  # already claimed or not available

    # atomic claim — only succeeds if scene is still unclaimed
    success = claim_scene(conn, scene_id, analyst_id)
    return success


def release_scene_to_pool(conn, scene_id, analyst_id):
    """analyst releases a scene they claimed back to the review pool"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] != 'in_review':
        raise ValueError("Only scenes in review can be released")
    if scene['claimed_by'] != analyst_id:
        raise ValueError("You can only release scenes you claimed")
    release_scene(conn, scene_id)


def force_release_scene(conn, scene_id, supervisor_id, comments=""):
    """supervisor force-releases a stuck claim back to the pool"""
    scene = get_scene_by_id(conn, scene_id)
    if scene['status'] != 'in_review':
        raise ValueError("Only scenes in review can be force-released")
    release_scene(conn, scene_id)
    log_review(conn, scene_id, supervisor_id, 'admin', 'force_released',
               comments or f"Claim by user {scene['claimed_by']} force-released")