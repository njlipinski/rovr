# app/models.py
"""plain Python dataclasses that represent core objects (user, scene, review, etc.)"""
from dataclasses import dataclass
from typing import Optional


class SceneStatus:
    UNCLAIMED          = 0
    CLAIMED            = 1
    PENDING_REVIEW     = 2
    IN_REVIEW          = 3
    NEEDS_REVISION     = 4
    PENDING_SUPERVISOR = 5
    APPROVED           = 6

    LABELS = {
        0: 'unclaimed',
        1: 'claimed',
        2: 'pending review',
        3: 'in review',
        4: 'needs revision',
        5: 'pending supervisor',
        6: 'approved',
    }


class Decision:
    APPROVE          = 'approve'
    REQUEST_REVISION = 'request_revision'
    SUBMITTED        = 'submitted'
    APPROVED         = 'approved'
    NEEDS_REVISION   = 'needs_revision'
    FORCE_RELEASED   = 'force_released'
    STATUS_OVERRIDE  = 'status_override'
    RESET            = 'reset'

    VALID_REVIEW = (APPROVE, REQUEST_REVISION)


class Stage:
    SUBMISSION        = 'submission'
    RESUBMISSION      = 'resubmission'
    PEER_REVIEW       = 'peer_review'
    SUPERVISOR_REVIEW = 'supervisor_review'
    ADMIN             = 'admin'


class Role:
    ANALYST    = 'analyst'
    SUPERVISOR = 'supervisor'


@dataclass
class User:
    id: int
    username: str
    active: bool
    password_hash: str
    role: str


@dataclass
class Scene:
    id: int
    name: str
    roi_filename: str                       # path to the .sel file on the R: drive
    owner_id: int                           # analyst 1 — who originally claimed and drew the scene; never changes
    assigned_to: int
    status: int
    peer_reviewer_id: Optional[int] = None
    supervisor_id: Optional[int] = None
    claimed_by: Optional[int] = None


@dataclass
class Review:
    id: int
    scene_id: int
    reviewer_id: int
    timestamp: str
    stage: str
    decision: str
    comments: Optional[str] = None
