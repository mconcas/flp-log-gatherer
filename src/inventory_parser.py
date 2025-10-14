"""
Ansible inventory parser for INI-style hosts files
"""
import re
from typing import Dict, List, Set
from pathlib import Path


class InventoryParser:
    """Parse Ansible inventory files in INI format"""

    def __init__(self, inventory_path: str):
        """
        Initialize the inventory parser

        Args:
            inventory_path: Path to the Ansible hosts file
        """
        self.inventory_path = Path(inventory_path)
        self.groups: Dict[str, List[str]] = {}
        self.node_to_groups: Dict[str, Set[str]] = {}
        # Pattern to match Ansible host expansion: hostname[start:end]
        self.expansion_pattern = re.compile(r'^([^[]+)\[(\d+):(\d+)\](.*)$')

    def parse(self) -> Dict[str, List[str]]:
        """
        Parse the inventory file

        Returns:
            Dictionary mapping group names to lists of hosts
        """
        if not self.inventory_path.exists():
            raise FileNotFoundError(
                f"Inventory file not found: {self.inventory_path}")

        current_group = None
        group_pattern = re.compile(r'^\[([^\]]+)\]')

        with open(self.inventory_path, 'r') as f:
            for line in f:
                line = line.strip()

                # Skip empty lines and comments
                if not line or line.startswith('#') or line.startswith(';'):
                    continue

                # Check for group header
                match = group_pattern.match(line)
                if match:
                    current_group = match.group(1)
                    if current_group not in self.groups:
                        self.groups[current_group] = []
                    continue

                # Add host to current group
                if current_group:
                    # Skip special ansible patterns like :children or :vars
                    if ':' in current_group or line.startswith('['):
                        continue

                    # Expand host patterns (e.g., hostname[001:004])
                    expanded_hosts = self._expand_host_pattern(line)

                    for host in expanded_hosts:
                        self.groups[current_group].append(host)

                        # Build reverse mapping (node -> groups)
                        if host not in self.node_to_groups:
                            self.node_to_groups[host] = set()
                        self.node_to_groups[host].add(current_group)

        return self.groups

    def _expand_host_pattern(self, host_line: str) -> List[str]:
        """
        Expand Ansible-style host patterns like 'hostname[start:end]'

        Args:
            host_line: Host line that may contain expansion pattern

        Returns:
            List of expanded hostnames, or single hostname if no pattern
        """
        # Split the line to get just the host part (before any variables)
        host_part = host_line.split()[0]

        match = self.expansion_pattern.match(host_part)
        if not match:
            # No expansion pattern, return as-is
            return [host_part]

        prefix = match.group(1)
        start_str = match.group(2)
        end_str = match.group(3)
        suffix = match.group(4) if match.group(4) else ""

        try:
            start = int(start_str)
            end = int(end_str)
        except ValueError:
            # Invalid range, return as-is
            return [host_part]

        # Handle invalid range (end < start)
        if end < start:
            return [host_part]

        # Determine zero-padding width from the start string
        padding_width = len(start_str)

        # Generate expanded hostnames
        expanded = []
        for i in range(start, end + 1):
            # Format with zero-padding to match original width
            padded_num = str(i).zfill(padding_width)
            hostname = f"{prefix}{padded_num}{suffix}"
            expanded.append(hostname)

        return expanded

    def get_groups(self) -> Dict[str, List[str]]:
        """
        Get all groups and their hosts

        Returns:
            Dictionary mapping group names to lists of hosts
        """
        return self.groups

    def get_hosts_in_group(self, group_name: str) -> List[str]:
        """
        Get all hosts in a specific group

        Args:
            group_name: Name of the group

        Returns:
            List of hostnames in the group
        """
        return self.groups.get(group_name, [])

    def get_groups_for_host(self, hostname: str) -> Set[str]:
        """
        Get all groups that a host belongs to

        Args:
            hostname: Name of the host

        Returns:
            Set of group names the host belongs to
        """
        return self.node_to_groups.get(hostname, set())

    def get_all_hosts(self) -> List[str]:
        """
        Get all unique hosts across all groups

        Returns:
            List of all hostnames
        """
        return list(self.node_to_groups.keys())


if __name__ == "__main__":
    # Example usage
    parser = InventoryParser("config/hosts")
    try:
        groups = parser.parse()
        print("Groups found:")
        for group, hosts in groups.items():
            print(f"  [{group}]")
            for host in hosts:
                print(f"    - {host}")
    except FileNotFoundError as e:
        print(f"Error: {e}")
