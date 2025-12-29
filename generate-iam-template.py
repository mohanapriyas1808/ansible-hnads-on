#!/usr/bin/env python3
import json
import yaml
import getpass
import subprocess
import sys
from datetime import datetime

def get_user_input():
    """Collect user input for IAM user creation"""
    users = []

    while True:
        print("\n--- IAM User Creation ---")
        username = input("Enter username: ").strip()
        if not username:
            print("Username cannot be empty!")
            continue

        expiration_days = input("Enter password expiration in days: ").strip()
        try:
            expiration_days = int(expiration_days)
            if expiration_days <= 0:
                print("Days must be a positive number!")
                continue
        except ValueError:
            print("Invalid number! Enter a positive integer")
            continue

        print("\nCommon AWS managed policies:")
        print("- ReadOnlyAccess")
        print("- PowerUserAccess")
        print("- IAMReadOnlyAccess")
        print("- S3FullAccess")
        print("- EC2FullAccess")
        policy_name = input("Enter AWS policy name (without arn): ").strip()
        if not policy_name:
            print("Policy name cannot be empty!")
            continue

        while True:
            password = getpass.getpass("Enter password for user (min 12 chars, must include: uppercase, lowercase, number, symbol): ")
            if not password:
                print("Password cannot be empty!")
                continue
            if len(password) < 12:
                print("Password must be at least 12 characters long!")
                continue
            if not any(c.isupper() for c in password):
                print("Password must contain at least one uppercase letter!")
                continue
            if not any(c.islower() for c in password):
                print("Password must contain at least one lowercase letter!")
                continue
            if not any(c.isdigit() for c in password):
                print("Password must contain at least one number!")
                continue
            if not any(c in '!@#$%^&*()_+-=[]{}|;:,.<>?' for c in password):
                print("Password must contain at least one symbol (!@#$%^&*()_+-=[]{}|;:,.<>?)!")
                continue
            break

        users.append({
            'username': username,
            'expiration_days': expiration_days,
            'policy_name': policy_name,
            'password': password
        })

        another = input("Add another user? (y/n): ").lower()
        if another != 'y':
            break

    return users

def create_ansible_playbook(users, max_pw_age):
    """Generate Ansible playbook template"""
    playbook = {
        'name': 'Create IAM Users with Password Expiration',
        'hosts': 'localhost',
        'gather_facts': False,
        'vars_files': ['vault.yml'],
        'tasks': []
    }

    # Set password policy first
    password_policy_task = {
        'name': 'Set account password policy',
        'amazon.aws.iam_password_policy': {
            'min_pw_length': 12,
            'require_symbols': True,
            'require_numbers': True,
            'require_uppercase': True,
            'require_lowercase': True,
            'allow_pw_change': True,
            'pw_max_age': max_pw_age,
            'pw_reuse_prevent': 3,
            'state': 'present'
        }
    }
    playbook['tasks'].append(password_policy_task)

    for user in users:
        # Check if user exists
        check_user_task = {
            'name': f"Check if user {user['username']} exists",
            'amazon.aws.iam_user_info': {
                'name': user['username']
            },
            'register': f"{user['username']}_exists",
            'ignore_errors': True
        }

        # Create user task (only if doesn't exist)
        create_user_task = {
            'name': f"Create IAM user {user['username']}",
            'amazon.aws.iam_user': {
                'name': user['username'],
                'password': f"{{{{ vault_{user['username']}_password }}}}",
                'update_password': 'on_create',
                'state': 'present'
            },
            'when': f"{user['username']}_exists.failed"
        }

        # Update existing user password
        update_password_task = {
            'name': f"Update password for existing user {user['username']}",
            'amazon.aws.iam_user': {
                'name': user['username'],
                'password': f"{{{{ vault_{user['username']}_password }}}}",
                'update_password': 'always',
                'state': 'present'
            },
            'when': f"not {user['username']}_exists.failed",
            'ignore_errors': True
        }

        # Attach policy task
        attach_policy_task = {
            'name': f"Attach policy to {user['username']}",
            'amazon.aws.iam_user': {
                'name': user['username'],
                'managed_policies': [f"arn:aws:iam::aws:policy/{user['policy_name']}"],
                'state': 'present'
            }
        }

        playbook['tasks'].extend([
            check_user_task, 
            create_user_task, 
            update_password_task, 
            attach_policy_task
        ])

    return playbook

def create_vault_file(users, vault_password):
    """Create encrypted vault file with passwords"""
    vault_data = {}

    for user in users:
        vault_data[f"vault_{user['username']}_password"] = user['password']

    # Write vault data to temporary file
    with open('temp_vault.yml', 'w') as f:
        yaml.dump(vault_data, f, default_flow_style=False)

    # Encrypt with ansible-vault
    try:
        process = subprocess.Popen(
            ['ansible-vault', 'encrypt', 'temp_vault.yml', '--output', 'vault.yml'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = process.communicate(input=f"{vault_password}\n{vault_password}\n")

        if process.returncode != 0:
            print(f"Error encrypting vault: {stderr}")
            return False

        # Remove temporary file
        subprocess.run(['rm', 'temp_vault.yml'])
        return True

    except FileNotFoundError:
        print("ansible-vault not found. Please install Ansible.")
        return False

def main():
    print("IAM User Creation Script")
    print("=" * 30)

    # Get user input
    users = get_user_input()

    if not users:
        print("No users to create. Exiting.")
        return

    # Get vault password
    print("\n--- Vault Encryption ---")
    vault_password = getpass.getpass("Enter vault password for encryption: ")
    if not vault_password:
        print("Vault password cannot be empty!")
        return

    # Create vault file
    print("Creating encrypted vault file...")
    if not create_vault_file(users, vault_password):
        print("Failed to create vault file. Exiting.")
        return

    # Get max password age (use the first user's expiration days for account policy)
    max_pw_age = users[0]['expiration_days']

    # Create playbook
    print("Creating Ansible playbook...")
    playbook_data = [create_ansible_playbook(users, max_pw_age)]

    with open('create_iam_users.yml', 'w') as f:
        yaml.dump(playbook_data, f, default_flow_style=False, indent=2)

    print("\nFiles created successfully:")
    print("- create_iam_users.yml (Ansible playbook)")
    print("- vault.yml (Encrypted passwords)")
    print("\nTo run the playbook:")
    print("ansible-playbook create_iam_users.yml --ask-vault-pass")

if __name__ == "__main__":
    main()
