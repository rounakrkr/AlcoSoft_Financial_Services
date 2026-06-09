#!/usr/bin/env python3
import sys
import os
import secrets
import string
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash

# Ensure we can import core modules
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.auth_manager import initialize_auth_db, _get_conn, log_auth_event

def generate_secure_password(length=16):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(secrets.choice(alphabet) for i in range(length))

def add_user(username, password, role):
    if role not in ['admin', 'viewer']:
        print("Error: Role must be 'admin' or 'viewer'.")
        return
    
    initialize_auth_db()
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), role, datetime.now().isoformat())
            )
            conn.commit()
        print(f"✅ User '{username}' created successfully as {role}.")
        log_auth_event("USER_CREATED", "CLI_ADMIN", True, f"Created {role} user: {username}")
    except sqlite3.IntegrityError:
        print(f"Error: User '{username}' already exists.")

def remove_user(username):
    try:
        with _get_conn() as conn:
            cursor = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.commit()
            if cursor.rowcount > 0:
                print(f"✅ User '{username}' deleted.")
                log_auth_event("USER_DELETED", "CLI_ADMIN", True, f"Deleted user: {username}")
            else:
                print(f"User '{username}' not found.")
    except Exception as e:
        print(f"Error: {e}")

def reset_password(username, new_password):
    try:
        with _get_conn() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (generate_password_hash(new_password), username)
            )
            conn.commit()
            if cursor.rowcount > 0:
                print(f"✅ Password reset for '{username}'.")
                log_auth_event("PASSWORD_RESET", "CLI_ADMIN", True, f"Reset password for user: {username}")
            else:
                print(f"User '{username}' not found.")
    except Exception as e:
        print(f"Error: {e}")

def list_users():
    initialize_auth_db()
    try:
        with _get_conn() as conn:
            rows = conn.execute("SELECT id, username, role, created_at FROM users").fetchall()
            print("\n📋 Registered Users:")
            print("-" * 50)
            for r in rows:
                print(f"ID: {r['id']} | {r['username']} ({r['role']}) - Created: {r['created_at']}")
            print("-" * 50)
    except Exception as e:
        print(f"Error: {e}")

def emergency_admin():
    initialize_auth_db()
    # Generate unique suffix
    suffix = secrets.token_hex(4)
    username = f"recovery_admin_{suffix}"
    password = generate_secure_password(20)
    
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), 'emergency_admin', datetime.now().isoformat())
            )
            conn.commit()
        print("\n🚨 EMERGENCY ADMIN ACCOUNT CREATED 🚨")
        print("This account will auto-expire 24 hours after creation.\n")
        print(f"Username : {username}")
        print(f"Password : {password}")
        print("\nSAVE THIS NOW. It will never be shown again.")
        log_auth_event("EMERGENCY_ADMIN_CREATED", "CLI", True, f"Created {username}")
    except Exception as e:
        print(f"Error creating emergency admin: {e}")

def print_help():
    print("AlcoSoft Offline User Management")
    print("Usage:")
    print("  python manage_users.py add <username> <password> <role>  (role: admin/viewer)")
    print("  python manage_users.py remove <username>")
    print("  python manage_users.py reset_password <username> <new_password>")
    print("  python manage_users.py list")
    print("  python manage_users.py emergency-admin")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)
        
    command = sys.argv[1].lower()
    
    if command == "add" and len(sys.argv) == 5:
        add_user(sys.argv[2], sys.argv[3], sys.argv[4].lower())
    elif command == "remove" and len(sys.argv) == 3:
        remove_user(sys.argv[2])
    elif command == "reset_password" and len(sys.argv) == 4:
        reset_password(sys.argv[2], sys.argv[3])
    elif command == "list":
        list_users()
    elif command == "emergency-admin":
        emergency_admin()
    else:
        print_help()
