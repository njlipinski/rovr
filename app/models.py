# app/models.py
"""plain Python dataclasses that represent core objects (user, scene, review, etc.)"""
from dataclasses import dataclass
from typing import Optional

@dataclass
class SceneStatus:
    UNCLAIMED = 0
    CLAIMED = 1
    PENDING_REVIEW = 2
    IN_REVIEW = 3
    NEEDS_REVISION = 4
    PENDING_SUPERVISOR = 5
    APPROVED = 6
    NEEDS_ATTENTION = 7

    LABELS = {
        0: 'unclaimed',
        1: 'claimed',
        2: 'pending review',
        3: 'in review',
        4: 'needs revision',
        5: 'pending supervisor',
        6: 'approved',
        7: 'needs attention',
    }

@dataclass
class User:
    id: int
    username: str  # same as display name, must be unique
    active: bool   # whether the user account is active or deactivated
    password_hash: str
    role: str      # 'analyst' or 'supervisor'

@dataclass
class Scene:
    id: int
    name: str
    roi_filename: str   # path to the .sel file on the Rice network drive
    owner_id: int       # analyst 1 — who originally claimed and drew the scene; never changes
    assigned_to: int    # current responsible analyst; updated only on supervisor reassignment
    status: int         # 0–7, see SceneStatus
    peer_reviewer_id: Optional[int] = None  # analyst 2 — set when peer review decision is submitted
    supervisor_id: Optional[int] = None     # set when supervisor decision is submitted
    claimed_by: Optional[int] = None        # holds claim lock for status 1 and 3; NULL otherwise

@dataclass
class Review:
    id: int
    scene_id: int
    reviewer_id: int
    timestamp: str          # when the review was made
    stage: str              # 'submission', 'resubmission', 'peer_review', 'supervisor_review', 'reassignment', 'admin'
    decision: str           # 'submitted', 'approved', 'needs_revision', 'reassigned', 'force_released'
    comments: Optional[str] = None  # required on kickback; NULL on approve
