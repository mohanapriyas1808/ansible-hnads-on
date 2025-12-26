#!/usr/bin/env python3

import os
import yaml
import json
import re
import time
import shutil
from pathlib import Path


def get_os_family(os_type):
    """Map OS type to Ansible OS family"""
    os_family_map = {
        'ubuntu': 'Debian',
        'debian': 'Debian',
        'centos': 'RedHat',
        'rhel': 'RedHat',
        'amazonlinux': 'RedHat',
        'fedora': 'RedHat',
        'windows': 'Windows'
    }
    return os_family_map.get(os_type.lower(), 'Linux')


def get_user_input():
    """Collect user input for Ansible template generation"""
    print("=== Ansible Software Installation Template Generator (SSM) ===\n")

    bucket_name = input("S3 bucket name for SSM sessions: ").strip()
    if not bucket_name:
        print("S3 bucket is required!")
        return None, None

    servers = []
    while True:
        print(f"\nEnter details for server {len(servers) + 1}:")
        hostname = input("Hostname: ").strip()
        if not hostname:
            break

        instance_id = input("Instance ID (i-xxxxxxxxx): ").strip()
        region = input("AWS Region (e.g., us-east-1): ").strip()
        os_type = input(
            "OS Type (ubuntu/centos/rhel/amazonlinux/windows): "
        ).strip().lower()

        server = {
            'hostname': hostname,
            'instance_id': instance_id,
            'region': region,
            'os_type': os_type,
            'os_family': get_os_family(os_type),
            'bucket_name': bucket_name,
            'software': []
        }

        print(f"\nSoftware for {hostname}:")
        while True:
            software = input("Software name (press Enter to finish): ").strip()
            if not software:
                break

            version = input(f"Version for {software} (optional): ").strip()
            install_method = input(
                "Installation method (package/snap/script/manual): "
            ).strip().lower() or "package"

            info = {'name': software, 'install_method': install_method}
            if version and version.lower() != "latest":
                info['version'] = version

            if install_method == 'package':
                if input("Custom repository? (y/n): ").strip().lower() == 'y':
                    print("Enter repository baseurl (e.g., https://download.docker.com/linux/centos/$releasever/$basearch/stable)")
                    repo_url = input("Repository baseurl: ").strip()
                    key_url = input("GPG Key URL (optional): ").strip()
                    package_name = input("Package name: ").strip() or software
                    info['custom_repo'] = {
                        'repo_url': repo_url,
                        'package_name': package_name
                    }
                    if key_url:
                        info['custom_repo']['key_url'] = key_url

            elif install_method == 'script':
                info['script_url'] = input("Installation script URL: ").strip()

            elif install_method == 'manual':
                info['download_url'] = input("Download URL: ").strip()
                info['install_path'] = input(
                    f"Install path (default /opt/{software}): "
                ).strip() or f"/opt/{software}"

            server['software'].append(info)

        servers.append(server)
        if input("Add another server? (y/n): ").strip().lower() != 'y':
            break

    # Group software by name and collect all configurations
    software_groups = {}
    for server in servers:
        for software in server['software']:
            name = software['name']
            if name not in software_groups:
                software_groups[name] = {
                    'configs': [],
                    'servers': []
                }
            # Store server with its specific software config
            software_groups[name]['servers'].append({
                'server': server,
                'config': software
            })
            # Collect unique configurations
            if software not in software_groups[name]['configs']:
                software_groups[name]['configs'].append(software)

    return servers, list(software_groups.values())


def generate_inventory(software_groups):
    inventory = {'all': {'children': {}}}

    for software_group in software_groups:
        software_name = software_group['configs'][0]['name']
        group = f"{software_name}_servers"
        inventory['all']['children'][group] = {'hosts': {}}

        for server_config in software_group['servers']:
            server = server_config['server']
            inventory['all']['children'][group]['hosts'][server['hostname']] = {
                'ansible_connection': 'aws_ssm',
                'ansible_aws_ssm_instance_id': server['instance_id'],
                'ansible_aws_ssm_region': server['region']
            }

    return inventory


def generate_playbook(software_groups):
    return [{
        'name': f"Install {group['configs'][0]['name']}",
        'hosts': f"{group['configs'][0]['name']}_servers",
        'become': True,
        'tasks': [{
            'name': f"Include {group['configs'][0]['name']} tasks",
            'include_tasks': f"tasks/{group['configs'][0]['name']}.yml"
        }]
    } for group in software_groups]


def generate_software_tasks(software_group):
    tasks = []
    configs = software_group['configs']
    
    # Group by OS family if multiple configs exist
    os_configs = {}
    for server_config in software_group['servers']:
        os_family = server_config['server']['os_family']
        config = server_config['config']
        if os_family not in os_configs:
            os_configs[os_family] = config
    
    # If multiple OS families, create conditional tasks
    if len(os_configs) > 1:
        for os_family, config in os_configs.items():
            tasks.extend(generate_os_specific_tasks(config, os_family))
    else:
        # Single configuration for all servers
        config = list(os_configs.values())[0]
        tasks.extend(generate_basic_tasks(config))
    
    return tasks


def generate_os_specific_tasks(software, os_family):
    method = software.get('install_method', 'package')
    tasks = []
    
    if method == 'package':
        if 'custom_repo' in software:
            # Add repository first
            if software['custom_repo'].get('key_url'):
                tasks.append({
                    'name': f"Add GPG key for {software['name']}",
                    'rpm_key': {
                        'key': software['custom_repo']['key_url']
                    },
                    'when': f"ansible_os_family == '{os_family}'"
                })
            
            tasks.append({
                'name': f"Add repository for {software['name']}",
                'yum_repository': {
                    'name': f"{software['name']}-repo",
                    'description': f"{software['name']} Repository",
                    'baseurl': software['custom_repo']['repo_url'],
                    'enabled': True,
                    'gpgcheck': True if software['custom_repo'].get('key_url') else False
                },
                'when': f"ansible_os_family == '{os_family}'"
            })
            
            # Update package cache after adding repository
            tasks.append({
                'name': f"Update package cache for {software['name']}",
                'yum': {
                    'update_cache': True
                },
                'when': f"ansible_os_family == '{os_family}'"
            })
        
        package_name = software.get('custom_repo', {}).get('package_name', software['name'])
        tasks.append({
            'name': f"Install {software['name']} on {os_family}",
            'package': {
                'name': package_name,
                'state': 'latest'
            },
            'when': f"ansible_os_family == '{os_family}'"
        })
    
    elif method == 'script':
        tasks.extend([
            {
                'name': f"Download script for {software['name']} on {os_family}",
                'get_url': {
                    'url': software['script_url'],
                    'dest': f"/tmp/{software['name']}.sh",
                    'mode': '0755'
                },
                'when': f"ansible_os_family == '{os_family}'"
            },
            {
                'name': f"Run script for {software['name']} on {os_family}",
                'shell': f"/tmp/{software['name']}.sh",
                'when': f"ansible_os_family == '{os_family}'"
            }
        ])
    
    return tasks


def generate_basic_tasks(software):
    method = software.get('install_method', 'package')
    tasks = []

    if method == 'package':
        if 'custom_repo' in software:
            # Add repository first
            if software['custom_repo'].get('key_url'):
                tasks.append({
                    'name': f"Add GPG key for {software['name']}",
                    'rpm_key': {
                        'key': software['custom_repo']['key_url']
                    }
                })
            
            tasks.append({
                'name': f"Add repository for {software['name']}",
                'yum_repository': {
                    'name': f"{software['name']}-repo",
                    'description': f"{software['name']} Repository",
                    'baseurl': software['custom_repo']['repo_url'],
                    'enabled': True,
                    'gpgcheck': True if software['custom_repo'].get('key_url') else False
                }
            })
            
            # Update package cache after adding repository
            tasks.append({
                'name': f"Update package cache for {software['name']}",
                'yum': {
                    'update_cache': True
                }
            })
        
        package_name = software.get('custom_repo', {}).get('package_name', software['name'])
        tasks.append({
            'name': f"Install {software['name']}",
            'package': {
                'name': package_name,
                'state': 'latest'
            }
        })

    elif method == 'snap':
        tasks.append({
            'name': f"Install {software['name']} via snap",
            'snap': {'name': software['name']}
        })

    elif method == 'script':
        tasks.extend([
            {
                'name': f"Download script for {software['name']}",
                'get_url': {
                    'url': software['script_url'],
                    'dest': f"/tmp/{software['name']}.sh",
                    'mode': '0755'
                }
            },
            {
                'name': f"Run script for {software['name']}",
                'shell': f"/tmp/{software['name']}.sh"
            }
        ])

    elif method == 'manual':
        tasks.extend([
            {
                'name': "Create install directory",
                'file': {
                    'path': software['install_path'],
                    'state': 'directory',
                    'mode': '0755'
                }
            },
            {
                'name': "Download and extract",
                'unarchive': {
                    'src': software['download_url'],
                    'dest': software['install_path'],
                    'remote_src': True
                }
            }
        ])

    return tasks


def create_files(servers, software_groups):
    base_dir = Path.home() / "ansible-software-install"
    base_dir.mkdir(exist_ok=True)
    (base_dir / "tasks").mkdir(exist_ok=True)

    inventory = generate_inventory(software_groups)
    with open(base_dir / "inventory.yml", "w") as f:
        yaml.dump(inventory, f)

    playbook = generate_playbook(software_groups)
    with open(base_dir / "install-software.yml", "w") as f:
        yaml.dump(playbook, f)

    for group in software_groups:
        tasks = generate_software_tasks(group)
        with open(base_dir / "tasks" / f"{group['configs'][0]['name']}.yml", "w") as f:
            yaml.dump(tasks, f)

    print("\nAnsible project generated successfully!")


def main():
    try:
        servers, software_groups = get_user_input()
        if not servers or not software_groups:
            print("Nothing to generate. Exiting.")
            return
        create_files(servers, software_groups)
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
