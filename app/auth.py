# auth.py
import bcrypt
from app.db import get_user_by_username

"""handles login and password logic, including hashing and salting passwords, and verifying login credentials"""


def authenticate_user(conn, username, password):
# only allow login if user is active and password is correct
    user = get_user_by_username(conn, username)
    if user and user['active'] and verify_password(password, user['password_hash']): 
        return user
    return None

def hash_password(password):
    """hashes a password using bcrypt"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

def verify_password(password, password_hash):
    """verifies a password against a hash"""
    return bcrypt.checkpw(password.encode('utf-8'), password_hash)
