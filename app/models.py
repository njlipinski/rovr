# app/models.py
from dataclasses import dataclass
from typing import Optional

"""plain Python dataclasses that represent core objects (user, scene, review, etc.)"""

@dataclass
class SceneStatus:
    DRAFT = 'draft'
    PENDING_REVIEW = 'pending_review'
    PENDING_SUPERVISOR = 'pending_supervisor'
    NEEDS_REVISION = 'needs_revision'
    APPROVED = 'approved'
    NEEDS_ATTENTION = 'needs_attention'
    
@dataclass
class User:
    id: int
    username: str # same as display name, must be unique
    active: bool # whether the user account is active or deactivated
    password_hash: str # TODO: might move later
    role: str  # 'analyst' or 'supervisor'
    
@dataclass
class Scene:
    id: int
    name: str
    roi_filename: str  # filename of the .sel file for this scene, stored in Rice network folder 
    owner_id: int  # user id of analyst 1 who created the scene
    assigned_to: int # user id of current analyst or supervisor assigned to review the scene (could be the same as owner_id if it's still with analyst 1)
    status: str  # 'draft', 'pending_review', 'pending_supervisor', 'needs_revision', 'approved', 'needs_attention' (if something goes wrong & need to reassign scene)
    # TODO: currently once it's rejected, it goes back to analyst 1 for revision. We could add a 'revised' status if we don't want to send it through peer analysis again.
    
@dataclass
class Review:
    id: int
    scene_id: int
    reviewer_id: int
    timestamp: str # when the review was made
    stage: str  # 'peer' or 'supervisor'
    decision: str  # 'approve' or 'reject'
    comments: Optional[str] = None# notes for analyst 1 if rejected
