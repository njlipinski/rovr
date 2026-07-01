# app/models.py
"""plain Python dataclasses that represent core objects (user, scene, review, etc.)"""
from dataclasses import dataclass
from typing import Optional


class SceneStatus:
    UNCLAIMED            = 0
    CLAIMED              = 1
    PENDING_REVIEW       = 2
    IN_REVIEW            = 3
    NEEDS_REVISION       = 4
    PENDING_SUPERVISOR   = 5
    IN_SUPERVISOR_REVIEW = 6
    APPROVED             = 7
    ISSUES               = 8

    LABELS = {
        0: 'unclaimed',
        1: 'claimed',
        2: 'pending review',
        3: 'in review',
        4: 'needs revision',
        5: 'pending supervisor',
        6: 'in supervisor review',
        7: 'approved',
        8: 'issues',
    }


class SceneFlag:
    OTHER       = 0
    BAD_SCENE   = 1
    BAD_FILTERS = 2

    LABELS = {0: "Other", 1: "Bad scene", 2: "Bad filters"}
    COLORS = {0: "#4A90D9", 1: "#E05A5A", 2: "#F5A623"}

    @staticmethod
    def parse(s):
        """Parse '{0,1,2}' → set of ints."""
        s = (s or '{}').strip('{}').strip()
        if not s:
            return set()
        return {int(x) for x in s.split(',')}

    @staticmethod
    def serialize(flags_set):
        """Serialize set of ints → '{0,1,2}'."""
        if not flags_set:
            return '{}'
        return '{' + ','.join(str(f) for f in sorted(flags_set)) + '}'


class Decision:
    APPROVE          = 'approve'
    REQUEST_REVISION = 'request_revision'
    SUBMITTED        = 'submitted'
    APPROVED         = 'approved'
    NEEDS_REVISION   = 'needs_revision'
    FORCE_RELEASED   = 'force_released'
    STATUS_OVERRIDE  = 'status_override'
    RESET            = 'reset'
    FLAG_UPDATED     = 'flag_updated'
    SCENE_EDITED     = 'scene_edited'
    MARKED_ISSUES    = 'marked_issues'

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
    roi_filename: str
    owner_id: int
    status: int
    flags: str = '{}'
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
