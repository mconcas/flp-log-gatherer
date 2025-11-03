"""
ProbeManager - Network connectivity and SSH availability testing.

This module provides functionality to probe remote hosts for:
- Network connectivity (ICMP ping)
- SSH connection availability

Designed to be lightweight and non-intrusive to avoid DDoS-like behavior.
"""

import asyncio
import logging
import subprocess
import time
import socket
import functools
from typing import Dict, List, Tuple

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


class ProbeManager:
    """Manages probing of remote hosts for connectivity and SSH access."""

    def __init__(self, ssh_user: str = None, strict_host_key_checking: bool = True,
                 gateway_host: str = None, gateway_user: str = None, gateway_port: int = 22,
                 retry_count: int = 3, retry_delay: int = 2):
        """
        Initialize the probe manager.

        Args:
            ssh_user: SSH username for connections (None uses default)
            strict_host_key_checking: Whether to enforce strict host key checking
            gateway_host: SSH gateway/jump host (None disables gateway)
            gateway_user: SSH username for gateway (None uses ssh_user)
            gateway_port: SSH port for gateway connection
            retry_count: Number of retry attempts for failed connections
            retry_delay: Delay in seconds between retry attempts
        """
        self.ssh_user = ssh_user
        self.strict_host_key_checking = strict_host_key_checking
        self.gateway_host = gateway_host
        self.gateway_user = gateway_user or ssh_user
        self.gateway_port = gateway_port
        self.retry_count = retry_count
        self.retry_delay = retry_delay

    async def probe_host(self, hostname: str) -> Dict[str, any]:
        """
        Probe a single host for ping and SSH connectivity.

        Args:
            hostname: The hostname or IP to probe

        Returns:
            Dictionary with probe results:
            {
                'hostname': str,
                'ping_success': bool,
                'ping_time_ms': float or None,
                'ssh_success': bool,
                'ssh_error': str or None
            }
        """
        logger.debug(f"Probing host: {hostname}")

        # Pre-resolve hostname to reduce DNS requests
        resolved_hostname = resolve_hostname(hostname)
        logger.debug(f"Resolved {hostname} to {resolved_hostname}")

        # Test ping
        ping_success, ping_time = await self._test_ping(hostname)

        # Test SSH
        ssh_success, ssh_error = await self._test_ssh(hostname)

        return {
            'hostname': hostname,
            'ping_success': ping_success,
            'ping_time_ms': ping_time,
            'ssh_success': ssh_success,
            'ssh_error': ssh_error
        }

    async def probe_hosts(self, hostnames: List[str]) -> List[Dict[str, any]]:
        """
        Probe multiple hosts in parallel.
        Uses batched SSH connections through gateway when configured to minimize gateway load.

        Args:
            hostnames: List of hostnames to probe

        Returns:
            List of probe result dictionaries
        """
        logger.info(f"Probing {len(hostnames)} hosts...")

        # Pre-resolve all hostnames to reduce DNS requests
        logger.debug("Pre-resolving hostnames to reduce DNS lookups...")
        for hostname in hostnames:
            resolve_hostname(hostname)

        # If gateway is configured, use batched approach to minimize gateway connections
        if self.gateway_host and len(hostnames) > 1:
            logger.info(f"Using batched SSH probing through gateway {self.gateway_host} to reduce connection load")
            return await self._probe_hosts_batched(hostnames)
        else:
            # No gateway or single host - use individual connections
            tasks = [self.probe_host(hostname) for hostname in hostnames]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Handle any exceptions
            final_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Error probing {hostnames[i]}: {result}")
                    final_results.append({
                        'hostname': hostnames[i],
                        'ping_success': False,
                        'ping_time_ms': None,
                        'ssh_success': False,
                        'ssh_error': str(result)
                    })
                else:
                    final_results.append(result)

            return final_results

    async def _probe_hosts_batched(self, hostnames: List[str]) -> List[Dict[str, any]]:
        """
        Probe multiple hosts using a single SSH connection through the gateway.
        This minimizes gateway connection overhead and avoids rate limiting.

        Args:
            hostnames: List of hostnames to probe

        Returns:
            List of probe result dictionaries
        """
        results = []
        
        # First, do ping tests in parallel (these don't use SSH)
        ping_tasks = [self._test_ping(hostname) for hostname in hostnames]
        ping_results = await asyncio.gather(*ping_tasks, return_exceptions=True)
        
        # Process ping results
        ping_dict = {}
        for i, ping_result in enumerate(ping_results):
            hostname = hostnames[i]
            if isinstance(ping_result, Exception):
                ping_dict[hostname] = (False, None)
            else:
                ping_dict[hostname] = ping_result

        # Now do batched SSH tests through gateway
        ssh_results = await self._test_ssh_batched(hostnames)
        
        # Combine results
        for hostname in hostnames:
            ping_success, ping_time = ping_dict.get(hostname, (False, None))
            ssh_success, ssh_error = ssh_results.get(hostname, (False, "Unknown error"))
            
            results.append({
                'hostname': hostname,
                'ping_success': ping_success,
                'ping_time_ms': ping_time,
                'ssh_success': ssh_success,
                'ssh_error': ssh_error
            })

        return results

    async def _test_ssh_batched(self, hostnames: List[str]) -> Dict[str, Tuple[bool, str]]:
        """
        Test SSH connectivity to multiple hosts in a single SSH session through gateway.
        This dramatically reduces gateway connection overhead.

        Args:
            hostnames: List of hostnames to test

        Returns:
            Dictionary mapping hostname to (success, error_message) tuple
        """
        results = {}
        last_error = None

        for attempt in range(1, self.retry_count + 1):
            try:
                logger.debug(f"Batched SSH test through gateway (attempt {attempt}/{self.retry_count}) for {len(hostnames)} hosts")

                # Build SSH command to gateway
                cmd = ['ssh']

                # Add user if specified
                if self.gateway_user:
                    cmd.extend(['-l', self.gateway_user])

                # Add SSH options (including connection multiplexing for efficiency)
                cmd.extend([
                    '-o', 'ConnectTimeout=10',
                    '-o', 'BatchMode=yes',
                    '-o', 'LogLevel=ERROR',
                    '-o', 'ControlMaster=auto',
                    '-o', 'ControlPath=/tmp/ssh-%r@%h:%p',
                    '-o', 'ControlPersist=300'
                ])

                if not self.strict_host_key_checking:
                    cmd.extend([
                        '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null'
                    ])

                # Connect to gateway
                gateway_target = f"{self.gateway_host}"
                if self.gateway_port != 22:
                    cmd.extend(['-p', str(self.gateway_port)])

                # Create a script that tests SSH to each target host from the gateway
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
                    
                    if not self.strict_host_key_checking:
                        target_cmd.extend([
                            '-o', 'StrictHostKeyChecking=no',
                            '-o', 'UserKnownHostsFile=/dev/null'
                        ])
                    
                    target_cmd.extend([hostname, 'echo', 'SSH_OK'])
                    
                    # Add the SSH command to the script (escape properly for shell)
                    escaped_cmd = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in target_cmd)
                    script_parts.append(f'{escaped_cmd} 2>&1')

                # Combine all commands into a single script
                batch_script = ' ; '.join(script_parts)
                cmd.extend([gateway_target, batch_script])

                logger.info(f"Making single SSH connection to gateway for {len(hostnames)} host probes (attempt {attempt})")

                # Execute the batched command
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                stdout, stderr = await proc.communicate()

                if proc.returncode == 0:
                    # Parse the batched output
                    results = self._parse_batched_ssh_output(hostnames, stdout.decode('utf-8', errors='replace'), separator)
                    logger.debug(f"Batched SSH test completed successfully for {len(results)} hosts")
                    return results
                else:
                    last_error = f"Gateway SSH failed (code {proc.returncode}): {stderr.decode('utf-8', errors='replace')}"
                    logger.warning(f"Batched SSH test failed: {last_error}")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"Batched SSH test attempt {attempt} failed: {last_error}")

            # Wait before retry if not the last attempt
            if attempt < self.retry_count:
                logger.info(f"Retrying batched SSH test in {self.retry_delay}s...")
                await asyncio.sleep(self.retry_delay)

        # All attempts failed - return failure for all hosts
        logger.error(f"All batched SSH test attempts failed. Last error: {last_error}")
        return {hostname: (False, last_error or "SSH test failed") for hostname in hostnames}

    def _parse_batched_ssh_output(self, hostnames: List[str], output: str, separator: str) -> Dict[str, Tuple[bool, str]]:
        """
        Parse the output from batched SSH command.

        Args:
            hostnames: List of hostnames that were tested
            output: Combined stdout from all SSH tests
            separator: Separator used to distinguish between host results

        Returns:
            Dictionary mapping hostname to (success, error_message) tuple
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
            
            if 'SSH_OK' in output_text:
                results[hostname] = (True, None)
                logger.debug(f"[{hostname}] SSH test successful (batched)")
            else:
                # Extract error message
                error_msg = output_text.strip() if output_text.strip() else "SSH connection failed"
                results[hostname] = (False, error_msg)
                logger.debug(f"[{hostname}] SSH test failed (batched): {error_msg}")

        return results

    async def _test_ping(self, hostname: str) -> Tuple[bool, float]:
        """
        Test ICMP ping connectivity.

        If gateway is configured, ping will be executed through the gateway.
        Otherwise, ping is executed locally.

        Args:
            hostname: The hostname to ping

        Returns:
            Tuple of (success: bool, avg_time_ms: float or None)
        """
        try:
            if self.gateway_host:
                # Ping through gateway using SSH
                return await self._test_ping_through_gateway(hostname)
            else:
                # Direct ping
                return await self._test_ping_direct(hostname)
        except Exception as e:
            logger.debug(f"[{hostname}] Ping test failed: {e}")
            return False, None

    async def _test_ping_direct(self, hostname: str) -> Tuple[bool, float]:
        """
        Test direct ICMP ping connectivity.

        Sends 1 initial ping. If it fails, returns immediately.
        If it succeeds, sends up to 4 more pings with 1s interval.

        Args:
            hostname: The hostname to ping

        Returns:
            Tuple of (success: bool, avg_time_ms: float or None)
        """
        try:
            # First ping - quick fail if host is unreachable
            logger.debug(f"[{hostname}] Testing initial direct ping...")
            cmd = ['ping', '-c', '1', '-W', '2', hostname]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.warning(
                    f"[{hostname}] Initial ping failed, skipping further pings")
                return False, None

            # First ping succeeded, do 4 more with 1s interval
            logger.debug(
                f"[{hostname}] Initial ping succeeded, sending 4 more...")
            cmd = ['ping', '-c', '4', '-i', '1', '-W', '2', hostname]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # Parse average time from output
                output = stdout.decode()
                for line in output.split('\n'):
                    if 'rtt min/avg/max/mdev' in line or 'round-trip' in line:
                        # Format: rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms
                        parts = line.split('=')
                        if len(parts) > 1:
                            times = parts[1].strip().split()[0]
                            avg_time = float(times.split('/')[1])
                            logger.debug(
                                f"[{hostname}] Direct ping successful: {avg_time:.2f}ms")
                            return True, avg_time

                # If we can't parse the time, still return success
                logger.debug(
                    f"[{hostname}] Direct ping successful (time not parsed)")
                return True, None
            else:
                logger.debug(f"[{hostname}] Direct ping failed")
                return False, None

        except Exception as e:
            logger.debug(f"[{hostname}] Direct ping test failed: {e}")
            return False, None

    async def _test_ping_through_gateway(self, hostname: str) -> Tuple[bool, float]:
        """
        Test ping connectivity through SSH gateway.

        Executes ping command on the gateway server to test connectivity to target host.

        Args:
            hostname: The hostname to ping from the gateway

        Returns:
            Tuple of (success: bool, avg_time_ms: float or None)
        """
        try:
            logger.debug(
                f"[{hostname}] Testing ping through gateway {self.gateway_host}...")

            # Build SSH command to execute ping on gateway
            ssh_cmd = ['ssh']

            # Add SSH options (including connection multiplexing for efficiency)
            ssh_cmd.extend([
                '-o', 'ConnectTimeout=10',
                '-o', 'BatchMode=yes',
                '-o', 'LogLevel=ERROR',
                '-o', 'ControlMaster=auto',
                '-o', 'ControlPath=/tmp/ssh-%r@%h:%p',
                '-o', 'ControlPersist=300'
            ])

            if not self.strict_host_key_checking:
                ssh_cmd.extend([
                    '-o', 'StrictHostKeyChecking=no',
                    '-o', 'UserKnownHostsFile=/dev/null'
                ])

            # Add gateway connection details
            if self.gateway_user:
                ssh_cmd.extend(['-l', self.gateway_user])
            ssh_cmd.extend(['-p', str(self.gateway_port)])
            ssh_cmd.append(self.gateway_host)

            # Ping command to execute on gateway
            ping_cmd = f"ping -c 4 -i 1 -W 2 {hostname}"
            ssh_cmd.append(ping_cmd)

            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # Parse average time from output
                output = stdout.decode()
                for line in output.split('\n'):
                    if 'rtt min/avg/max/mdev' in line or 'round-trip' in line:
                        # Format: rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms
                        parts = line.split('=')
                        if len(parts) > 1:
                            times = parts[1].strip().split()[0]
                            avg_time = float(times.split('/')[1])
                            logger.debug(
                                f"[{hostname}] Gateway ping successful: {avg_time:.2f}ms")
                            return True, avg_time

                # If we can't parse the time, still return success
                logger.debug(
                    f"[{hostname}] Gateway ping successful (time not parsed)")
                return True, None
            else:
                # Check if it's a gateway connection issue vs target unreachable
                stderr_output = stderr.decode()
                if any(keyword in stderr_output.lower() for keyword in ['connection refused', 'connection timed out', 'host unreachable']):
                    logger.debug(
                        f"[{hostname}] Gateway connection failed: {stderr_output}")
                else:
                    logger.debug(
                        f"[{hostname}] Ping through gateway failed (target unreachable)")
                return False, None

        except Exception as e:
            logger.debug(f"[{hostname}] Gateway ping test failed: {e}")
            return False, None

    async def _test_ssh(self, hostname: str) -> Tuple[bool, str]:
        """
        Test SSH connection availability with retry logic.

        Attempts to establish SSH connection with a simple command.
        Retries on transient connection failures.

        Args:
            hostname: The hostname to connect to

        Returns:
            Tuple of (success: bool, error_message: str or None)
        """
        last_error = None

        for attempt in range(1, self.retry_count + 1):
            try:
                logger.debug(
                    f"[{hostname}] Testing SSH connection (attempt {attempt}/{self.retry_count})...")

                # Build SSH command
                cmd = ['ssh']

                # Add user if specified
                if self.ssh_user:
                    cmd.extend(['-l', self.ssh_user])

                # Add SSH options (including connection multiplexing for efficiency)
                cmd.extend([
                    '-o', 'ConnectTimeout=10',
                    '-o', 'BatchMode=yes',
                    '-o', 'LogLevel=ERROR',
                    '-o', 'ControlMaster=auto',
                    '-o', 'ControlPath=/tmp/ssh-%r@%h:%p',
                    '-o', 'ControlPersist=300'
                ])

                if not self.strict_host_key_checking:
                    cmd.extend([
                        '-o', 'StrictHostKeyChecking=no',
                        '-o', 'UserKnownHostsFile=/dev/null'
                    ])

                # Add gateway/proxy jump configuration if specified
                if self.gateway_host:
                    cmd.extend([
                        '-o', f'ProxyJump={self.gateway_user}@{self.gateway_host}:{self.gateway_port}'
                    ])
                    if attempt == 1:  # Only log gateway info on first attempt
                        logger.debug(
                            f"[{hostname}] Using gateway: {self.gateway_user}@{self.gateway_host}:{self.gateway_port}")

                # Add hostname and simple command
                cmd.extend([hostname, 'echo', 'SSH_OK'])

                # Execute SSH command
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                stdout, stderr = await proc.communicate()

                if proc.returncode == 0 and b'SSH_OK' in stdout:
                    if attempt > 1:
                        logger.info(
                            f"[{hostname}] SSH connection successful on attempt {attempt}")
                    else:
                        logger.debug(f"[{hostname}] SSH connection successful")
                    return True, None
                else:
                    error_msg = stderr.decode().strip(
                    ) if stderr else f"Exit code: {proc.returncode}"
                    last_error = error_msg

                    # Check if this is a transient error that should be retried
                    is_transient_error = any(indicator in error_msg.lower() for indicator in [
                        'connection closed by remote host',
                        'kex_exchange_identification',
                        'connection reset',
                        'connection timed out',
                        'temporary failure',
                        'resource temporarily unavailable'
                    ])

                    if is_transient_error and attempt < self.retry_count:
                        logger.warning(
                            f"[{hostname}] Transient SSH error (attempt {attempt}/{self.retry_count}): {error_msg}")
                        logger.info(
                            f"[{hostname}] Retrying SSH connection in {self.retry_delay}s...")
                        await asyncio.sleep(self.retry_delay)
                        continue
                    elif not is_transient_error:
                        # Non-transient error, don't retry
                        logger.warning(
                            f"[{hostname}] SSH connection failed (non-transient): {error_msg}")
                        return False, error_msg
                    else:
                        # Last attempt failed
                        logger.warning(
                            f"[{hostname}] SSH connection failed after {attempt} attempts: {error_msg}")
                        return False, error_msg

            except Exception as e:
                last_error = str(e)
                logger.error(
                    f"[{hostname}] SSH error on attempt {attempt}: {e}")
                if attempt < self.retry_count:
                    logger.info(
                        f"[{hostname}] Retrying SSH connection in {self.retry_delay}s...")
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    return False, last_error

        return False, last_error or "Max retries exceeded"

    @staticmethod
    def print_probe_results(results: List[Dict[str, any]], gateway_info: Dict[str, str] = None):
        """
        Print probe results in a compact table format.

        Args:
            results: List of probe result dictionaries
            gateway_info: Optional gateway configuration info to display
        """
        print("\n" + "=" * 100)
        print("PROBE RESULTS")
        if gateway_info:
            print(
                f"Gateway: {gateway_info['user']}@{gateway_info['host']}:{gateway_info['port']}")
        print("=" * 100)

        # Calculate column widths
        max_hostname_len = max(len(r['hostname']) for r in results)
        hostname_width = max(max_hostname_len, 20)

        # Print header
        header = f"{'HOSTNAME':<{hostname_width}}  {'PING':<15}  {'SSH':<15}  {'NOTES'}"
        print(header)
        print("-" * 100)

        # Print results
        for result in sorted(results, key=lambda x: x['hostname']):
            hostname = result['hostname']

            # Format ping status
            if result['ping_success']:
                if result['ping_time_ms'] is not None:
                    ping_status = f"\033[92m✓ {result['ping_time_ms']:.1f}ms\033[0m"
                else:
                    ping_status = f"\033[92m✓ OK\033[0m"
            else:
                ping_status = "\033[91m✗ FAILED\033[0m"

            # Format SSH status
            if result['ssh_success']:
                ssh_status = "\033[92m✓ OK\033[0m"
            else:
                ssh_status = "\033[91m✗ FAILED\033[0m"

            # Format notes
            notes = ""
            if not result['ssh_success'] and result.get('ssh_error'):
                error = result['ssh_error']
                # Truncate long error messages
                if len(error) > 40:
                    error = error[:37] + "..."
                notes = error

            # Print row
            # Remove ANSI codes from length calculation
            ping_display_len = len(result['ping_time_ms'] is not None and f"✓ {result['ping_time_ms']:.1f}ms" or (
                result['ping_success'] and "✓ OK" or "✗ FAILED"))
            ssh_display_len = len(
                result['ssh_success'] and "✓ OK" or "✗ FAILED")

            print(
                f"{hostname:<{hostname_width}}  {ping_status:<{15+10}}  {ssh_status:<{15+10}}  {notes}")

        # Print summary
        total = len(results)
        ping_ok = sum(1 for r in results if r['ping_success'])
        ssh_ok = sum(1 for r in results if r['ssh_success'])

        print("-" * 100)
        print(f"Summary: {ping_ok}/{total} ping OK, {ssh_ok}/{total} SSH OK")
        print("=" * 100)
