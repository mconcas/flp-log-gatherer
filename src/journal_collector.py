"""
Journal collector - extracts systemd journal logs from remote nodes
with minimal impact on the remote system
"""
import asyncio
import logging
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime, timedelta
import subprocess

logger = logging.getLogger(__name__)


class JournalCollector:
    """Collect systemd journal logs with minimal remote impact"""

    def __init__(
        self,
        ssh_user: str = "root",
        ssh_port: int = 22,
        ssh_ignore_host_key: bool = True,
        timeout: int = 300,
        output_format: str = "short",
        enable_ssh_compression: bool = True
    ):
        """
        Initialize the journal collector

        Args:
            ssh_user: SSH user for remote connections
            ssh_port: SSH port
            ssh_ignore_host_key: Whether to ignore SSH host key verification
            timeout: Timeout in seconds for journal extraction
            output_format: journalctl output format (short, short-iso, short-iso-precise, json)
            enable_ssh_compression: Enable SSH compression to reduce network load
        """
        self.ssh_user = ssh_user
        self.ssh_port = ssh_port
        self.ssh_ignore_host_key = ssh_ignore_host_key
        self.timeout = timeout
        self.output_format = output_format
        self.enable_ssh_compression = enable_ssh_compression

    def _build_ssh_command(self, hostname: str) -> List[str]:
        """
        Build SSH command with appropriate options

        Args:
            hostname: Target hostname

        Returns:
            SSH command as list of arguments
        """
        ssh_cmd = [
            "ssh",
            f"{self.ssh_user}@{hostname}",
            "-p", str(self.ssh_port),
        ]

        # Enable SSH compression to reduce network load (minimal CPU impact)
        if self.enable_ssh_compression:
            ssh_cmd.extend(["-C"])

        if self.ssh_ignore_host_key:
            ssh_cmd.extend([
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR"
            ])

        return ssh_cmd

    def _build_journalctl_command(
        self,
        unit: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        priority: Optional[str] = None,
        lines: Optional[int] = None,
        boot: Optional[int] = None
    ) -> str:
        """
        Build journalctl command with filters to minimize remote impact

        Args:
            unit: Systemd unit to filter (e.g., 'sshd', 'nginx', 'postgresql')
            since: Start time (e.g., '1 day ago', '2023-01-01 00:00:00')
            until: End time
            priority: Log priority filter (e.g., 'err', 'warning')
            lines: Number of lines to retrieve (default: all)
            boot: Boot number (0 = current boot, -1 = previous boot)

        Returns:
            journalctl command string
        """
        cmd_parts = ["journalctl"]

        # Output format: 'short' is lighter, 'short-iso-precise' includes precise timestamps
        # 'json' for structured data (heavier but parseable)
        cmd_parts.append(f"--output={self.output_format}")

        # No pager (critical for remote execution)
        cmd_parts.append("--no-pager")

        # Quiet mode - suppresses hint messages (reduces output slightly)
        cmd_parts.append("--quiet")

        # Filters to minimize data transfer and remote CPU usage
        if unit:
            cmd_parts.append(f"--unit={unit}")

        if since:
            cmd_parts.append(f"--since='{since}'")

        if until:
            cmd_parts.append(f"--until='{until}'")

        if priority:
            cmd_parts.append(f"--priority={priority}")

        if lines:
            cmd_parts.append(f"--lines={lines}")

        if boot is not None:
            cmd_parts.append(f"--boot={boot}")

        return " ".join(cmd_parts)

    async def collect_journal(
        self,
        hostname: str,
        local_path: Path,
        app_name: str,
        unit: Optional[str] = None,
        since_days: Optional[int] = None,
        priority: Optional[str] = None,
        current_boot_only: bool = True
    ) -> Dict[str, Any]:
        """
        Collect journal logs from a remote host

        Args:
            hostname: Remote hostname
            local_path: Local directory to store the journal export
            app_name: Application name (for logging and file naming)
            unit: Systemd unit to filter (None = all units)
            since_days: Only collect logs from the last N days
            priority: Filter by priority (err, warning, info, debug)
            current_boot_only: Only collect logs from current boot

        Returns:
            Dictionary with collection results
        """
        result = {
            'hostname': hostname,
            'app_name': app_name,
            'success': False,
            'output': '',
            'error': '',
            'lines_collected': 0,
            'file_size_bytes': 0
        }

        try:
            # Create local directory
            local_path.mkdir(parents=True, exist_ok=True)

            # Build since parameter
            since = None
            if since_days:
                since = f"{since_days} days ago"

            # Build journalctl command
            journalctl_cmd = self._build_journalctl_command(
                unit=unit,
                since=since,
                priority=priority,
                boot=0 if current_boot_only else None
            )

            # Build SSH command
            ssh_cmd = self._build_ssh_command(hostname)
            ssh_cmd.append(journalctl_cmd)

            logger.info(
                f"Collecting journal from {hostname} (app: {app_name}, unit: {unit})")
            logger.debug(f"Command: {' '.join(ssh_cmd)}")

            # Execute command
            process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )

                result['output'] = stdout.decode('utf-8', errors='replace')
                result['error'] = stderr.decode('utf-8', errors='replace')

                if process.returncode == 0:
                    # Save to file
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    unit_suffix = f"_{unit}" if unit else "_all"
                    filename = f"journal{unit_suffix}_{timestamp}.log"
                    output_file = local_path / filename

                    with open(output_file, 'w') as f:
                        f.write(result['output'])

                    result['success'] = True
                    result['lines_collected'] = len(
                        result['output'].splitlines())
                    result['file_size_bytes'] = output_file.stat().st_size
                    result['output_file'] = str(output_file)

                    logger.info(
                        f"Journal collected from {hostname}: "
                        f"{result['lines_collected']} lines, "
                        f"{result['file_size_bytes']} bytes -> {output_file}"
                    )
                else:
                    logger.error(
                        f"Failed to collect journal from {hostname}: "
                        f"exit code {process.returncode}, error: {result['error']}"
                    )

            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                result['error'] = f"Timeout after {self.timeout} seconds"
                logger.error(f"Journal collection from {hostname} timed out")

        except Exception as e:
            result['error'] = str(e)
            logger.error(
                f"Exception during journal collection from {hostname}: {e}")

        return result

    async def check_journal_available(self, hostname: str) -> bool:
        """
        Check if journalctl is available on the remote host

        Args:
            hostname: Remote hostname

        Returns:
            True if journalctl is available
        """
        try:
            ssh_cmd = self._build_ssh_command(hostname)
            ssh_cmd.append("which journalctl")

            process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            await asyncio.wait_for(process.communicate(), timeout=10)

            return process.returncode == 0

        except Exception as e:
            logger.warning(
                f"Could not check journalctl availability on {hostname}: {e}")
            return False

    async def list_available_units(self, hostname: str) -> List[str]:
        """
        List available systemd units on the remote host

        Args:
            hostname: Remote hostname

        Returns:
            List of unit names
        """
        units = []

        try:
            ssh_cmd = self._build_ssh_command(hostname)
            ssh_cmd.append(
                "systemctl list-units --type=service --no-pager --no-legend | awk '{print $1}'")

            process = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)

            if process.returncode == 0:
                units = stdout.decode(
                    'utf-8', errors='replace').strip().split('\n')
                units = [u.strip() for u in units if u.strip()]

        except Exception as e:
            logger.warning(f"Could not list units on {hostname}: {e}")

        return units

    def get_unit_name_for_app(self, app_name: str) -> Optional[str]:
        """
        Get likely systemd unit name for an application

        Args:
            app_name: Application name

        Returns:
            Systemd unit name or None for system-wide logs
        """
        # Map application names to common systemd unit names
        unit_mappings = {
            'nginx': 'nginx.service',
            'apache': 'apache2.service',
            'postgresql': 'postgresql.service',
            'mysql': 'mysql.service',
            'mariadb': 'mariadb.service',
            'redis': 'redis.service',
            'elasticsearch': 'elasticsearch.service',
            'docker': 'docker.service',
            'ssh': 'sshd.service',
            'systemd': 'systemd',
        }

        # For 'system' app, return None to collect all logs
        if app_name.lower() == 'system':
            return None

        return unit_mappings.get(app_name.lower(), f"{app_name}.service")


if __name__ == "__main__":
    # Example usage
    async def test():
        collector = JournalCollector(ssh_user="root")

        # Check if journalctl is available
        available = await collector.check_journal_available("localhost")
        print(f"journalctl available: {available}")

        if available:
            # List units
            units = await collector.list_available_units("localhost")
            print(f"Found {len(units)} units")

            # Collect some logs
            result = await collector.collect_journal(
                hostname="localhost",
                local_path=Path("/tmp/test_journal"),
                app_name="system",
                since_days=1,
                current_boot_only=True
            )
            print(f"Collection result: {result}")

    # asyncio.run(test())
