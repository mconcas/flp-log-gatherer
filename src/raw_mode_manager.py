"""
Raw mode manager for quick log directory size estimation.

This module provides functionality to quickly estimate total log storage
usage across all hosts by checking standard log directories and journal paths.
"""

import asyncio
import logging
import subprocess
import re
import socket
import functools
from typing import Dict, List, Tuple, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


# DNS Cache to reduce redundant lookups
@functools.lru_cache(maxsize=1000)
def resolve_hostname(hostname: str) -> str:
    """Cache DNS resolution to reduce redundant lookups"""
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        logger.warning(f"DNS resolution failed for {hostname}, using hostname as-is")
        return hostname


class RawModeManager:
    """Manages raw mode operations for quick log size estimation."""

    def __init__(self, config_manager, ssh_user: str = "root", ssh_port: int = 22,
                 ssh_ignore_host_key: bool = True, gateway_host: str = None,
                 gateway_user: str = None, gateway_port: int = 22,
                 retry_count: int = 3, retry_delay: int = 2, timeout: int = 30):
        """
        Initialize the raw mode manager.

        Args:
            config_manager: ConfigManager instance for accessing configuration
            ssh_user: SSH username for connections
            ssh_port: SSH port for connections
            ssh_ignore_host_key: Whether to ignore SSH host key checking
            gateway_host: SSH gateway/jump host (None disables gateway)
            gateway_user: SSH username for gateway (None uses ssh_user)
            gateway_port: SSH port for gateway connection
            retry_count: Number of retry attempts for failed connections
            retry_delay: Delay in seconds between retry attempts
            timeout: Timeout in seconds for SSH operations
        """
        self.config_manager = config_manager
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.ssh_ignore_host_key = ssh_ignore_host_key
        self.gateway_host = gateway_host
        self.gateway_user = gateway_user or ssh_user
        self.gateway_port = gateway_port
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.timeout = timeout

    def get_directories_to_check(self) -> List[str]:
        """
        Get the list of directories to check in raw mode.

        Returns:
            List of directory paths to check
        """
        directories = []
        
        # Get generic log directories from config
        raw_config = self.config_manager.config.get('raw_mode', {})
        generic_dirs = raw_config.get('generic_log_dirs', ['/var/log'])
        directories.extend(generic_dirs)
        
        # Add journal paths if configured
        if raw_config.get('include_journal_paths', True):
            journal_config = self.config_manager.config.get('journal_options', {})
            binary_config = journal_config.get('binary', {})
            journal_paths = binary_config.get('remote_journal_path', [])
            
            # Handle both single string and list formats
            if isinstance(journal_paths, str):
                journal_paths = [journal_paths]
            
            directories.extend(journal_paths)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_dirs = []
        for directory in directories:
            if directory not in seen:
                seen.add(directory)
                unique_dirs.append(directory)
        
        logger.debug(f"Raw mode will check directories: {unique_dirs}")
        return unique_dirs

    async def check_host_raw_sizes(self, hostnames: List[str]) -> Dict[str, Dict]:
        """
        Check raw directory sizes for multiple hosts using batched SSH connections.

        Args:
            hostnames: List of hostnames to check

        Returns:
            Dictionary mapping hostname to size information
        """
        directories = self.get_directories_to_check()
        
        if not directories:
            logger.warning("No directories configured for raw mode checking")
            return {}

        logger.info(f"Checking raw directory sizes on {len(hostnames)} hosts for {len(directories)} directories")
        
        # Pre-resolve all hostnames to reduce DNS requests
        logger.debug("Pre-resolving hostnames to reduce DNS lookups...")
        for hostname in hostnames:
            resolve_hostname(hostname)
        
        # Use batched approach if we have a gateway or many hosts
        if self.gateway_host or len(hostnames) > 1:
            return await self._check_hosts_batched(hostnames, directories)
        else:
            # Single host without gateway - use individual connection
            results = {}
            for hostname in hostnames:
                results[hostname] = await self._check_single_host(hostname, directories)
            return results

    async def _check_hosts_batched(self, hostnames: List[str], directories: List[str]) -> Dict[str, Dict]:
        """
        Check directory sizes using batched SSH connections through gateway.

        Args:
            hostnames: List of hostnames to check
            directories: List of directories to check on each host

        Returns:
            Dictionary mapping hostname to size information
        """
        results = {}
        last_error = None

        for attempt in range(1, self.retry_count + 1):
            try:
                logger.debug(f"Batched raw size check (attempt {attempt}/{self.retry_count}) for {len(hostnames)} hosts")

                if self.gateway_host:
                    # Check through gateway
                    batch_results = await self._check_through_gateway(hostnames, directories, attempt)
                else:
                    # Direct connections but batched for efficiency
                    batch_results = await self._check_direct_batched(hostnames, directories, attempt)
                
                if batch_results:
                    return batch_results

            except Exception as e:
                last_error = str(e)
                logger.warning(f"Batched raw size check attempt {attempt} failed: {last_error}")

                if attempt < self.retry_count:
                    logger.info(f"Retrying batched raw size check in {self.retry_delay}s...")
                    await asyncio.sleep(self.retry_delay)

        # All attempts failed
        logger.error(f"All batched raw size check attempts failed. Last error: {last_error}")
        return {hostname: self._create_error_result(last_error or "Unknown error") for hostname in hostnames}

    async def _check_through_gateway(self, hostnames: List[str], directories: List[str], attempt: int) -> Dict[str, Dict]:
        """
        Check directory sizes through SSH gateway.

        Args:
            hostnames: List of hostnames to check
            directories: List of directories to check
            attempt: Current attempt number

        Returns:
            Dictionary mapping hostname to size information
        """
        # Build SSH command to gateway
        cmd = ['ssh']

        if self.gateway_user:
            cmd.extend(['-l', self.gateway_user])

        cmd.extend([
            '-o', 'ConnectTimeout=10',
            '-o', 'BatchMode=yes',
            '-o', 'LogLevel=ERROR',
            '-o', 'ControlMaster=auto',
            '-o', 'ControlPath=/tmp/ssh-%r@%h:%p',
            '-o', 'ControlPersist=300'
        ])

        if self.ssh_ignore_host_key:
            cmd.extend([
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null'
            ])

        if self.gateway_port != 22:
            cmd.extend(['-p', str(self.gateway_port)])

        # Create a script that checks directory sizes on each target host
        separator = "=== HOST_SEPARATOR ==="
        script_parts = []

        for i, hostname in enumerate(hostnames):
            # Add separator and hostname identifier
            script_parts.append(f'echo "{separator}HOST_{i}:{hostname}"')

            # Build SSH command from gateway to target host
            target_cmd = ['ssh']
            if self.ssh_user:
                target_cmd.extend(['-l', self.ssh_user])

            target_cmd.extend([
                '-o', 'ConnectTimeout=10',
                '-o', 'BatchMode=yes',
                '-o', 'LogLevel=ERROR',
                '-o', 'ControlMaster=auto',
                '-o', 'ControlPath=/tmp/ssh-%r@%h:%p',
                '-o', 'ControlPersist=300'
            ])

            if self.ssh_ignore_host_key:
                target_cmd.extend([
                    '-o', 'StrictHostKeyChecking=no',
                    '-o', 'UserKnownHostsFile=/dev/null'
                ])

            # Create du command to check directory sizes
            du_dirs = ' '.join(f"'{d}'" for d in directories)
            du_cmd = f'du -sh {du_dirs} 2>/dev/null'
            
            target_cmd.extend([hostname, du_cmd])

            # Add the SSH command to the script (escape properly for shell)
            escaped_cmd = ' '.join(f"'{arg}'" if ' ' in arg or '"' in arg else arg for arg in target_cmd)
            script_parts.append(f'{escaped_cmd} 2>&1')

        # Combine all commands into a single script
        batch_script = ' ; '.join(script_parts)
        cmd.extend([self.gateway_host, batch_script])

        logger.info(f"Making single SSH connection to gateway for {len(hostnames)} host raw size checks (attempt {attempt})")

        # Execute the batched command
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=self.timeout * len(hostnames)
        )

        stdout_str = stdout.decode('utf-8', errors='replace')
        stderr_str = stderr.decode('utf-8', errors='replace')

        if proc.returncode == 0 or (proc.returncode != 0 and stdout_str.strip()):
            # Accept the result if we got some output, even with non-zero exit code
            # This happens when some directories don't exist but others do
            results = self._parse_batched_output(hostnames, directories, stdout_str, separator)
            logger.debug(f"Batched raw size check completed successfully for {len(results)} hosts")
            return results
        else:
            error_msg = f"Gateway SSH failed (code {proc.returncode}): {stderr_str}"
            logger.warning(f"Batched raw size check failed: {error_msg}")
            raise Exception(error_msg)

    async def _check_direct_batched(self, hostnames: List[str], directories: List[str], attempt: int) -> Dict[str, Dict]:
        """
        Check directory sizes using direct SSH connections (no gateway).

        Args:
            hostnames: List of hostnames to check
            directories: List of directories to check
            attempt: Current attempt number

        Returns:
            Dictionary mapping hostname to size information
        """
        # For direct connections, we'll still do them in parallel for efficiency
        tasks = []
        for hostname in hostnames:
            task = self._check_single_host(hostname, directories)
            tasks.append(task)

        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        results = {}
        for i, result in enumerate(results_list):
            hostname = hostnames[i]
            if isinstance(result, Exception):
                results[hostname] = self._create_error_result(str(result))
            else:
                results[hostname] = result

        return results

    async def _check_single_host(self, hostname: str, directories: List[str]) -> Dict:
        """
        Check directory sizes on a single host.

        Args:
            hostname: Hostname to check
            directories: List of directories to check

        Returns:
            Dictionary with size information for the host
        """
        cmd = ['ssh']

        if self.ssh_user:
            cmd.extend(['-l', self.ssh_user])

        cmd.extend([
            '-o', 'ConnectTimeout=10',
            '-o', 'BatchMode=yes',
            '-o', 'LogLevel=ERROR',
            '-o', 'ControlMaster=auto',
            '-o', 'ControlPath=/tmp/ssh-%r@%h:%p',
            '-o', 'ControlPersist=300'
        ])

        if self.ssh_ignore_host_key:
            cmd.extend([
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null'
            ])

        if self.ssh_port != 22:
            cmd.extend(['-p', str(self.ssh_port)])

        # Create du command to check directory sizes
        du_dirs = ' '.join(f"'{d}'" for d in directories)
        du_cmd = f'du -sh {du_dirs} 2>/dev/null'

        cmd.extend([hostname, du_cmd])

        logger.debug(f"[{hostname}] Checking raw directory sizes")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout
            )

            output = stdout.decode('utf-8', errors='replace')
            stderr_output = stderr.decode('utf-8', errors='replace')
            
            if proc.returncode == 0 or (proc.returncode != 0 and output.strip()):
                # Accept the result if we got some output, even with non-zero exit code
                # This happens when some directories don't exist but others do
                return self._parse_du_output(hostname, directories, output)
            else:
                error_msg = stderr_output or f"SSH command failed with code {proc.returncode}"
                return self._create_error_result(f"SSH failed: {error_msg}")

        except Exception as e:
            return self._create_error_result(f"Connection error: {str(e)}")

    def _parse_batched_output(self, hostnames: List[str], directories: List[str], output: str, separator: str) -> Dict[str, Dict]:
        """
        Parse the output from batched SSH command.

        Args:
            hostnames: List of hostnames that were checked
            directories: List of directories that were checked
            output: Combined stdout from all size checks
            separator: Separator used to distinguish between host results

        Returns:
            Dictionary mapping hostname to size information
        """
        results = {}

        # Split output by separator
        sections = output.split(separator)

        # Create a mapping of host index to results
        host_outputs = {}

        for section in sections:
            if not section.strip():
                continue

            lines = section.strip().split('\n')
            if not lines:
                continue

            # First line should be the host identifier
            first_line = lines[0]
            if first_line.startswith('HOST_'):
                try:
                    # Parse: HOST_0:hostname
                    parts = first_line.split(':', 1)
                    if len(parts) >= 2:
                        host_idx = int(parts[0].replace('HOST_', ''))
                        # Store the output for this host (excluding the identifier line)
                        host_outputs[host_idx] = '\n'.join(lines[1:]) if len(lines) > 1 else ''
                except (ValueError, IndexError):
                    continue

        # Process each hostname and create results
        for i, hostname in enumerate(hostnames):
            output_text = host_outputs.get(i, '')
            results[hostname] = self._parse_du_output(hostname, directories, output_text)

        return results

    def _parse_du_output(self, hostname: str, directories: List[str], output: str) -> Dict:
        """
        Parse du command output to extract directory sizes.

        Args:
            hostname: Hostname being processed
            directories: List of directories that were checked
            output: du command output

        Returns:
            Dictionary with parsed size information
        """
        if not output.strip() or "ERROR:" in output:
            logger.debug(f"[{hostname}] Raw size check failed or no output")
            return self._create_error_result("Could not check directories")

        directory_sizes = {}
        total_bytes = 0

        # Parse du output lines
        for line in output.strip().split('\n'):
            if not line.strip():
                continue

            # du output format: "SIZE\tDIRECTORY"
            parts = line.split('\t', 1)
            if len(parts) == 2:
                size_str, path = parts
                size_bytes = self._parse_size_string(size_str)
                directory_sizes[path] = {
                    'size_human': size_str,
                    'size_bytes': size_bytes
                }
                total_bytes += size_bytes

        if directory_sizes:
            logger.debug(f"[{hostname}] Raw size check successful: {self._human_readable_size(total_bytes)} total")
            return {
                'success': True,
                'directories': directory_sizes,
                'total_size_bytes': total_bytes,
                'total_size_human': self._human_readable_size(total_bytes),
                'error': None
            }
        else:
            logger.debug(f"[{hostname}] Raw size check found no directories")
            return {
                'success': True,
                'directories': {},
                'total_size_bytes': 0,
                'total_size_human': '0 B',
                'error': None
            }

    def _create_error_result(self, error_message: str) -> Dict:
        """
        Create an error result dictionary.

        Args:
            error_message: Error message to include

        Returns:
            Dictionary with error information
        """
        return {
            'success': False,
            'directories': {},
            'total_size_bytes': 0,
            'total_size_human': '0 B',
            'error': error_message
        }

    def _parse_size_string(self, size_str: str) -> int:
        """
        Parse a human-readable size string (from du) into bytes.

        Args:
            size_str: Size string like "1.5G", "500M", "10K"

        Returns:
            Size in bytes
        """
        size_str = size_str.strip().upper()
        
        # Extract number and unit
        match = re.match(r'^([\d.]+)([KMGTPE]?)$', size_str)
        if not match:
            return 0

        number_str, unit = match.groups()
        try:
            number = float(number_str)
        except ValueError:
            return 0

        # Convert to bytes
        multipliers = {
            '': 1024,  # du uses 1K blocks by default
            'K': 1024,
            'M': 1024 * 1024,
            'G': 1024 * 1024 * 1024,
            'T': 1024 * 1024 * 1024 * 1024,
            'P': 1024 * 1024 * 1024 * 1024 * 1024,
            'E': 1024 * 1024 * 1024 * 1024 * 1024 * 1024
        }

        return int(number * multipliers.get(unit, 1024))

    def _human_readable_size(self, size_bytes: int) -> str:
        """
        Convert bytes to human-readable format.

        Args:
            size_bytes: Size in bytes

        Returns:
            Human-readable string (e.g., "1.5 GB", "3.2 TB")
        """
        if size_bytes == 0:
            return "0 B"

        units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
        unit_index = 0
        size = float(size_bytes)

        while size >= 1024.0 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1

        if unit_index == 0:  # Bytes
            return f"{int(size)} {units[unit_index]}"
        else:
            return f"{size:.1f} {units[unit_index]}"

    def generate_raw_summary(self, results: Dict[str, Dict], output_file: str = None) -> None:
        """
        Generate a markdown summary of raw mode results.

        Args:
            results: Dictionary mapping hostname to size information
            output_file: Output file path for the summary (auto-generated with timestamp if None)
        """
        if output_file is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"SUMMARY_RAW_{timestamp}.md"
        total_bytes_all = 0
        successful_hosts = 0
        failed_hosts = 0
        
        # Calculate totals
        for hostname, result in results.items():
            if result.get('success', False):
                successful_hosts += 1
                total_bytes_all += result.get('total_size_bytes', 0)
            else:
                failed_hosts += 1

        # Generate markdown content
        content = f"""# Raw Log Directory Size Summary

## Overview

- **Total Hosts Checked**: {len(results)}
- **Successful Checks**: {successful_hosts}
- **Failed Checks**: {failed_hosts}
- **Total Storage Used**: {self._human_readable_size(total_bytes_all)}

## Per-Host Breakdown

| Hostname | Total Size | Status | Directories |
|----------|------------|--------|-------------|
"""

        # Add per-host details
        sorted_results = sorted(results.items(), key=lambda x: x[1].get('total_size_bytes', 0), reverse=True)
        
        for hostname, result in sorted_results:
            if result.get('success', False):
                total_size = result.get('total_size_human', '0 B')
                dir_count = len(result.get('directories', {}))
                status = "✓ OK"
                directories = f"{dir_count} directories"
            else:
                total_size = "N/A"
                status = "✗ FAILED"
                error = result.get('error', 'Unknown error')
                directories = f"Error: {error}"

            content += f"| {hostname} | {total_size} | {status} | {directories} |\n"

        content += f"""
## Directory Details

"""

        # Add detailed directory breakdown for successful hosts
        directories_summary = {}
        for hostname, result in results.items():
            if result.get('success', False):
                for dir_path, dir_info in result.get('directories', {}).items():
                    if dir_path not in directories_summary:
                        directories_summary[dir_path] = {
                            'total_bytes': 0,
                            'host_count': 0,
                            'hosts': []
                        }
                    directories_summary[dir_path]['total_bytes'] += dir_info.get('size_bytes', 0)
                    directories_summary[dir_path]['host_count'] += 1
                    directories_summary[dir_path]['hosts'].append(f"{hostname}: {dir_info.get('size_human', 'N/A')}")

        if directories_summary:
            content += "### Directory Totals Across All Hosts\n\n"
            content += "| Directory | Total Size | Hosts | Average per Host |\n"
            content += "|-----------|------------|-------|------------------|\n"

            for dir_path, info in sorted(directories_summary.items(), key=lambda x: x[1]['total_bytes'], reverse=True):
                total_size = self._human_readable_size(info['total_bytes'])
                host_count = info['host_count']
                avg_size = self._human_readable_size(info['total_bytes'] // max(host_count, 1))
                content += f"| `{dir_path}` | {total_size} | {host_count} | {avg_size} |\n"

        content += f"""

## Failed Hosts

"""
        failed_any = False
        for hostname, result in results.items():
            if not result.get('success', False):
                if not failed_any:
                    content += "| Hostname | Error |\n"
                    content += "|----------|-------|\n"
                    failed_any = True
                error = result.get('error', 'Unknown error')
                content += f"| {hostname} | {error} |\n"

        if not failed_any:
            content += "No failed hosts.\n"

        content += f"""

*Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""

        # Write to file
        with open(output_file, 'w') as f:
            f.write(content)

        logger.info(f"Raw mode summary written to: {output_file}")
        print(f"Raw mode summary written to: {output_file}")