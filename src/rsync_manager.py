"""
Rsync job manager for parallel log collection
"""
import subprocess
import asyncio
import logging
import re
import socket
import functools
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass


# DNS Cache to reduce redundant lookups
@functools.lru_cache(maxsize=1000)
def resolve_hostname(hostname: str) -> str:
    """Cache DNS resolution to reduce redundant lookups"""
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        logger.warning(f"DNS resolution failed for {hostname}, using hostname as-is")
        return hostname


# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def human_readable_size(size_bytes: int) -> str:
    """
    Convert bytes to human-readable format

    Args:
        size_bytes: Size in bytes

    Returns:
        Human-readable string (e.g., "1.5 MB", "3.2 GB")
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


def parse_ls_output(ls_output: str) -> List[Dict[str, any]]:
    """
    Parse ls -la output to extract file information
    Handles both directory listings and find+ls output

    Args:
        ls_output: Output from ls -la command or find+ls command

    Returns:
        List of dictionaries with file information
    """
    files = []
    lines = ls_output.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip total line from ls -la
        if line.startswith('total '):
            continue

        # Skip error messages
        if 'No such file or directory' in line or 'Permission denied' in line:
            continue

        # Parse ls -la format: permissions links owner group size date time name
        # Example: -rw-r--r-- 1 root root 1234 Oct 8 12:34 filename.log
        parts = line.split()

        if len(parts) < 9:  # Need at least 9 parts for a valid ls -la line
            continue

        permissions = parts[0]

        # Skip . and .. entries
        filename = ' '.join(parts[8:])  # Handle filenames with spaces
        if filename in ['.', '..']:
            continue

        # Skip if it's a directory (we want actual files for size calculation)
        is_directory = permissions.startswith('d')
        if is_directory:
            continue

        try:
            size_bytes = int(parts[4])

            # Get modification time (parts 5, 6, 7)
            mod_time = ' '.join(parts[5:8])

            files.append({
                'name': filename,
                'size_bytes': size_bytes,
                'size_human': human_readable_size(size_bytes),
                'permissions': permissions,
                'is_directory': is_directory,
                'mod_time': mod_time
            })
        except (ValueError, IndexError):
            # If we can't parse the line properly, skip it
            continue

    return files


@dataclass
class RsyncJob:
    """Represents a single rsync operation"""
    hostname: str
    app_name: str
    remote_path: str
    local_path: Path
    flags: List[str]
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_ignore_host_key: bool = True
    # Gateway/proxy configuration
    gateway_host: Optional[str] = None
    gateway_user: Optional[str] = None
    gateway_port: int = 22


@dataclass
class JobResult:
    """Result of a rsync job execution"""
    job: RsyncJob
    success: bool
    stdout: str
    stderr: str
    return_code: int
    duration: float
    attempts: int


class RsyncManager:
    """Manage parallel rsync job execution"""

    def __init__(self, max_parallel_jobs: int = 5, retry_count: int = 3,
                 retry_delay: int = 5, timeout: int = 300):
        """
        Initialize the rsync manager

        Args:
            max_parallel_jobs: Maximum number of concurrent rsync jobs
            retry_count: Number of retry attempts for failed jobs
            retry_delay: Delay in seconds between retries
            timeout: Timeout in seconds for each rsync operation
        """
        self.max_parallel_jobs = max_parallel_jobs
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.timeout = timeout
        self.results: List[JobResult] = []

    def _is_file_not_found_error(self, stderr_str: str, return_code: int) -> bool:
        """
        Check if the rsync error indicates files not found (non-retryable)
        
        Args:
            stderr_str: Standard error output from rsync
            return_code: Process return code
            
        Returns:
            True if this is a file-not-found error that should not be retried
        """
        # Common rsync file-not-found indicators
        file_not_found_patterns = [
            "No such file or directory",
            "file has vanished",
            "source files disappearing",
            "no files to consider",
            "cannot stat"
        ]
        
        # Return codes that indicate file issues (not connection issues)
        file_error_codes = {3, 23}  # 3: input/output file errors, 23: partial transfer
        
        # Check for file-not-found patterns in error message
        for pattern in file_not_found_patterns:
            if pattern in stderr_str:
                return True
                
        # Check for specific return codes that indicate file issues
        if return_code in file_error_codes:
            # Additional check: make sure it's not a connection issue disguised as file error
            connection_indicators = [
                "Connection refused",
                "Connection timed out", 
                "Host key verification failed",
                "Permission denied (publickey",
                "ssh: connect to host",
                "Network is unreachable"
            ]
            
            for conn_pattern in connection_indicators:
                if conn_pattern in stderr_str:
                    return False  # This is actually a connection issue, should retry
                    
            return True  # File error, don't retry
            
        return False  # Other errors may be transient, allow retry

    def build_rsync_command(self, job: RsyncJob, dry_run: bool = False) -> List[str]:
        """
        Build rsync command for a job

        Args:
            job: RsyncJob to build command for
            dry_run: If True, add --dry-run flag

        Returns:
            List of command arguments
        """
        # Ensure local directory exists
        job.local_path.mkdir(parents=True, exist_ok=True)

        # Build SSH connection string
        ssh_target = f"{job.ssh_user}@{job.hostname}"

        # Build rsync command
        cmd = ['rsync']

        # Add flags
        cmd.extend(job.flags)

        # Add dry-run flag if requested
        if dry_run:
            cmd.append('--dry-run')

        # Add verbose flag for better logging
        if '-v' not in job.flags and '--verbose' not in job.flags:
            cmd.append('-v')

        # Add SSH options (port, host key checking, etc.)
        ssh_opts = f"ssh -p {job.ssh_port}"
        if job.ssh_ignore_host_key:
            ssh_opts += " -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        
        # Add SSH connection multiplexing to reduce DNS lookups and connection overhead
        ssh_opts += " -o ControlMaster=auto -o ControlPath=/tmp/ssh-%r@%h:%p -o ControlPersist=300"

        # Add gateway/proxy jump host configuration if specified
        if job.gateway_host:
            gateway_user = job.gateway_user or job.ssh_user
            ssh_opts += f" -o ProxyJump={gateway_user}@{job.gateway_host}:{job.gateway_port}"
            logger.debug(
                f"[{job.hostname}/{job.app_name}] Using gateway: {gateway_user}@{job.gateway_host}:{job.gateway_port}")

        cmd.extend(['-e', ssh_opts])

        # Add source and destination
        # Note: rsync needs trailing slash handling
        remote_source = f"{ssh_target}:{job.remote_path}"
        cmd.append(remote_source)
        cmd.append(str(job.local_path) + '/')

        return cmd

    async def execute_job(self, job: RsyncJob, dry_run: bool = False) -> JobResult:
        """
        Execute a single rsync job with retry logic

        Args:
            job: RsyncJob to execute
            dry_run: If True, perform a dry-run

        Returns:
            JobResult with execution details
        """
        # Pre-resolve hostname to reduce DNS lookups during retries
        try:
            resolved_ip = resolve_hostname(job.hostname)
            logger.debug(f"[{job.hostname}] Resolved to {resolved_ip}")
        except Exception as e:
            logger.warning(f"[{job.hostname}] DNS pre-resolution failed: {e}")

        start_time = datetime.now()
        attempts = 0

        while attempts < self.retry_count:
            attempts += 1

            try:
                cmd = self.build_rsync_command(job, dry_run)
                logger.info(
                    f"[{job.hostname}/{job.app_name}] Starting rsync (attempt {attempts}/{self.retry_count})")
                logger.debug(f"[{job.hostname}/{job.app_name}] Command: {' '.join(cmd)}")

                # Execute rsync command
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(),
                        timeout=self.timeout
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    logger.error(
                        f"[{job.hostname}/{job.app_name}] Timeout after {self.timeout}s")
                    if attempts < self.retry_count:
                        logger.debug(
                            f"[{job.hostname}/{job.app_name}] Retrying in {self.retry_delay}s...")
                        await asyncio.sleep(self.retry_delay)
                        continue
                    else:
                        duration = (datetime.now() -
                                    start_time).total_seconds()
                        return JobResult(
                            job=job,
                            success=False,
                            stdout="",
                            stderr=f"Timeout after {self.timeout}s",
                            return_code=-1,
                            duration=duration,
                            attempts=attempts
                        )

                stdout_str = stdout.decode('utf-8', errors='replace')
                stderr_str = stderr.decode('utf-8', errors='replace')

                duration = (datetime.now() - start_time).total_seconds()

                if process.returncode == 0:
                    logger.info(
                        f"[{job.hostname}/{job.app_name}] ✓ Success ({duration:.1f}s)")
                    return JobResult(
                        job=job,
                        success=True,
                        stdout=stdout_str,
                        stderr=stderr_str,
                        return_code=process.returncode,
                        duration=duration,
                        attempts=attempts
                    )
                else:
                    # Check if this is a file-not-found error (don't retry)
                    if self._is_file_not_found_error(stderr_str, process.returncode):
                        logger.info(
                            f"[{job.hostname}/{job.app_name}] ✗ Files not found, skipping retries ({duration:.1f}s)")
                        logger.debug(f"[{job.hostname}/{job.app_name}] Error: {stderr_str.strip()}")
                        return JobResult(
                            job=job,
                            success=False,
                            stdout=stdout_str,
                            stderr=stderr_str,
                            return_code=process.returncode,
                            duration=duration,
                            attempts=attempts
                        )
                    elif attempts < self.retry_count:
                        logger.warning(
                            f"[{job.hostname}/{job.app_name}] ⚠ Failed with return code {process.returncode}, retrying... ({duration:.1f}s)")
                        logger.debug(f"[{job.hostname}/{job.app_name}] Error: {stderr_str.strip()}")
                        await asyncio.sleep(self.retry_delay)
                        continue
                    else:
                        logger.error(
                            f"[{job.hostname}/{job.app_name}] ✗ Failed after {attempts} attempts ({duration:.1f}s)")
                        logger.debug(f"[{job.hostname}/{job.app_name}] Final error: {stderr_str.strip()}")
                        return JobResult(
                            job=job,
                            success=False,
                            stdout=stdout_str,
                            stderr=stderr_str,
                            return_code=process.returncode,
                            duration=duration,
                            attempts=attempts
                        )

            except Exception as e:
                duration = (datetime.now() - start_time).total_seconds()
                if attempts < self.retry_count:
                    logger.warning(
                        f"[{job.hostname}/{job.app_name}] ⚠ Exception: {e}, retrying... ({duration:.1f}s)")
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    logger.error(
                        f"[{job.hostname}/{job.app_name}] ✗ Exception after {attempts} attempts ({duration:.1f}s): {e}")
                    return JobResult(
                        job=job,
                        success=False,
                        stdout="",
                        stderr=str(e),
                        return_code=-1,
                        duration=duration,
                        attempts=attempts
                    )

        # Should not reach here, but just in case
        duration = (datetime.now() - start_time).total_seconds()
        return JobResult(
            job=job,
            success=False,
            stdout="",
            stderr="Max retries exceeded",
            return_code=-1,
            duration=duration,
            attempts=attempts
        )

    async def check_remote_file_exists(self, job: RsyncJob) -> Tuple[bool, Dict]:
        """
        Check if remote files exist and get their information (explore mode)
        Includes retry logic for transient SSH failures

        Args:
            job: RsyncJob to check

        Returns:
            Tuple of (exists, file_info_dict)
            file_info_dict contains:
            - 'files': List of file information dictionaries
            - 'total_size_bytes': Total size of all files in bytes
            - 'total_size_human': Human-readable total size
            - 'file_count': Number of files found
            - 'error': Error message if any
            - 'ssh_error': True if this was an SSH connection error vs file not found
        """
        for attempt in range(1, self.retry_count + 1):
            try:
                exists, file_info = await self._check_remote_file_exists_single(job, attempt)
                return exists, file_info
            except (asyncio.TimeoutError, OSError, ConnectionError) as e:
                # Log SSH connection failures
                logger.warning(
                    f"[{job.hostname}/{job.app_name}] SSH connection failed "
                    f"(attempt {attempt}/{self.retry_count}): {str(e)}"
                )

                if attempt < self.retry_count:
                    logger.info(
                        f"[{job.hostname}/{job.app_name}] Retrying SSH connection in {self.retry_delay}s..."
                    )
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    # All retries failed - this is an SSH connection error
                    logger.error(
                        f"[{job.hostname}/{job.app_name}] SSH connection failed after {self.retry_count} attempts: {str(e)}"
                    )
                    file_info = {
                        'files': [],
                        'total_size_bytes': 0,
                        'total_size_human': '0 B',
                        'file_count': 0,
                        'raw_output': '',
                        'error': f"SSH connection failed after {self.retry_count} attempts: {str(e)}",
                        'ssh_error': True
                    }
                    return False, file_info
            except Exception as e:
                # Unexpected error
                logger.error(
                    f"[{job.hostname}/{job.app_name}] Unexpected error during SSH check: {str(e)}"
                )
                file_info = {
                    'files': [],
                    'total_size_bytes': 0,
                    'total_size_human': '0 B',
                    'file_count': 0,
                    'raw_output': '',
                    'error': f"Unexpected error: {str(e)}",
                    'ssh_error': True
                }
                return False, file_info

        # Should not reach here
        file_info = {
            'files': [],
            'total_size_bytes': 0,
            'total_size_human': '0 B',
            'file_count': 0,
            'raw_output': '',
            'error': "Max retries exceeded",
            'ssh_error': True
        }
        return False, file_info

    async def _check_remote_files_batched(self, jobs: List[RsyncJob]) -> List[Dict]:
        """
        Check multiple remote paths in a single SSH connection
        Includes retry logic for transient SSH failures

        Args:
            jobs: List of RsyncJobs for the same host

        Returns:
            List of result dictionaries, one per job
        """
        if not jobs:
            return []

        # Use the first job for connection parameters (they should all be the same for the same host)
        reference_job = jobs[0]
        
        for attempt in range(1, self.retry_count + 1):
            try:
                return await self._check_remote_files_batched_single(jobs, attempt)
            except (asyncio.TimeoutError, OSError, ConnectionError) as e:
                # Log SSH connection failures
                logger.warning(
                    f"[{reference_job.hostname}] SSH connection failed "
                    f"(attempt {attempt}/{self.retry_count}): {str(e)}"
                )

                if attempt < self.retry_count:
                    logger.warning(
                        f"[{reference_job.hostname}] Retrying SSH connection in {self.retry_delay}s... "
                        f"(will retry {len(jobs)} applications in single connection)"
                    )
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    # All retries failed - return SSH error for all jobs
                    logger.error(
                        f"[{reference_job.hostname}] SSH connection failed after {self.retry_count} attempts: {str(e)}"
                    )
                    results = []
                    for job in jobs:
                        file_info = {
                            'files': [],
                            'total_size_bytes': 0,
                            'total_size_human': '0 B',
                            'file_count': 0,
                            'raw_output': '',
                            'error': f"SSH connection failed after {self.retry_count} attempts: {str(e)}",
                            'ssh_error': True
                        }
                        results.append({
                            'job': job,
                            'exists': False,
                            'file_info': file_info
                        })
                    return results
            except Exception as e:
                # Unexpected error
                logger.error(
                    f"[{reference_job.hostname}] Unexpected error during batched SSH check: {str(e)}"
                )
                results = []
                for job in jobs:
                    file_info = {
                        'files': [],
                        'total_size_bytes': 0,
                        'total_size_human': '0 B',
                        'file_count': 0,
                        'raw_output': '',
                        'error': f"Unexpected error: {str(e)}",
                        'ssh_error': True
                    }
                    results.append({
                        'job': job,
                        'exists': False,
                        'file_info': file_info
                    })
                return results

        # Should not reach here - return error for all jobs
        results = []
        for job in jobs:
            file_info = {
                'files': [],
                'total_size_bytes': 0,
                'total_size_human': '0 B',
                'file_count': 0,
                'raw_output': '',
                'error': "Max retries exceeded",
                'ssh_error': True
            }
            results.append({
                'job': job,
                'exists': False,
                'file_info': file_info
            })
        return results

    async def _check_remote_files_batched_single(self, jobs: List[RsyncJob], attempt: int) -> List[Dict]:
        """
        Single attempt to check multiple remote paths in one SSH connection

        Args:
            jobs: List of RsyncJobs for the same host
            attempt: Current attempt number (for logging)

        Returns:
            List of result dictionaries, one per job
        """
        if not jobs:
            return []

        reference_job = jobs[0]
        ssh_target = f"{reference_job.ssh_user}@{reference_job.hostname}"

        # Build SSH command
        cmd = [
            'ssh',
            '-p', str(reference_job.ssh_port)
        ]

        # Add host key checking options if configured
        if reference_job.ssh_ignore_host_key:
            cmd.extend([
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                '-o', 'LogLevel=ERROR'
            ])

        # Add gateway/proxy jump host configuration if specified
        if reference_job.gateway_host:
            gateway_user = reference_job.gateway_user or reference_job.ssh_user
            cmd.extend([
                '-o', f'ProxyJump={gateway_user}@{reference_job.gateway_host}:{reference_job.gateway_port}'
            ])
            logger.debug(
                f"[{reference_job.hostname}] Using gateway for batched explore: {gateway_user}@{reference_job.gateway_host}:{reference_job.gateway_port}")

        # Create a script that checks all paths in one go
        # Use a unique separator to distinguish between different path results
        separator = "=== PATH_SEPARATOR ==="
        script_parts = []
        
        for i, job in enumerate(jobs):
            # Add separator and job identifier
            script_parts.append(f'echo "{separator}JOB_{i}:{job.app_name}:{job.remote_path}"')
            # Check if path exists and get file listing
            find_cmd = f'find {job.remote_path} -type f -exec ls -la {{}} \\; 2>/dev/null || ls -la {job.remote_path} 2>&1'
            script_parts.append(find_cmd)

        # Combine all commands into a single script
        batch_script = ' ; '.join(script_parts)
        cmd.extend([ssh_target, batch_script])

        logger.info(
            f"[{reference_job.hostname}] Making SSH connection for {len(jobs)} paths (attempt {attempt}/{self.retry_count})"
        )
        logger.debug(
            f"[{reference_job.hostname}] Checking {len(jobs)} remote paths in batched SSH (attempt {attempt})"
        )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=self.timeout * len(jobs)  # Scale timeout with number of paths
        )

        stdout_str = stdout.decode('utf-8', errors='replace')
        stderr_str = stderr.decode('utf-8', errors='replace')

        # Parse the batched output
        return self._parse_batched_output(jobs, stdout_str, stderr_str, separator)

    def _parse_batched_output(self, jobs: List[RsyncJob], stdout_str: str, stderr_str: str, separator: str) -> List[Dict]:
        """
        Parse the output from batched SSH command

        Args:
            jobs: List of RsyncJobs that were checked
            stdout_str: Combined stdout from all path checks
            stderr_str: Combined stderr
            separator: Separator used to distinguish between path results

        Returns:
            List of result dictionaries, one per job
        """
        results = []
        
        # Split output by separator
        sections = stdout_str.split(separator)
        
        # Create a mapping of job index to results
        job_outputs = {}
        
        for section in sections:
            if not section.strip():
                continue
                
            lines = section.strip().split('\n')
            if not lines:
                continue
                
            # First line should be the job identifier
            first_line = lines[0]
            if first_line.startswith('JOB_'):
                try:
                    # Parse: JOB_0:app_name:path
                    parts = first_line.split(':', 2)
                    if len(parts) >= 3:
                        job_idx = int(parts[0].replace('JOB_', ''))
                        # Store the output for this job (excluding the identifier line)
                        job_outputs[job_idx] = '\n'.join(lines[1:]) if len(lines) > 1 else ''
                except (ValueError, IndexError):
                    continue

        # Process each job and create results
        for i, job in enumerate(jobs):
            output = job_outputs.get(i, '')
            
            logger.debug(
                f"[{job.hostname}/{job.app_name}] Checking remote path: {job.remote_path} (batched)"
            )
            
            if not output.strip():
                # No output - path doesn't exist or no files
                logger.debug(f"[{job.hostname}/{job.app_name}] Path not found or no files: {job.remote_path}")
                file_info = {
                    'files': [],
                    'total_size_bytes': 0,
                    'total_size_human': '0 B',
                    'file_count': 0,
                    'raw_output': output,
                    'error': None,
                    'ssh_error': False
                }
                results.append({
                    'job': job,
                    'exists': False,
                    'file_info': file_info
                })
                continue

            # Check for error indicators
            if any(error in output.lower() for error in ['no such file', 'not found', 'permission denied']):
                logger.debug(f"[{job.hostname}/{job.app_name}] Path not found or no files: {job.remote_path}")
                file_info = {
                    'files': [],
                    'total_size_bytes': 0,
                    'total_size_human': '0 B',
                    'file_count': 0,
                    'raw_output': output,
                    'error': None,
                    'ssh_error': False
                }
                results.append({
                    'job': job,
                    'exists': False,
                    'file_info': file_info
                })
                continue

            # Parse file information
            file_list = parse_ls_output(output)
            total_size = sum(f['size_bytes'] for f in file_list if 'size_bytes' in f)

            if file_list:
                logger.debug(f"[{job.hostname}/{job.app_name}] Found {len(file_list)} files, {human_readable_size(total_size)}")
                file_info = {
                    'files': file_list,
                    'total_size_bytes': total_size,
                    'total_size_human': human_readable_size(total_size),
                    'file_count': len(file_list),
                    'raw_output': output,
                    'error': None,
                    'ssh_error': False
                }
                results.append({
                    'job': job,
                    'exists': True,
                    'file_info': file_info
                })
            else:
                # Output exists but no files parsed - directory exists but empty or ls failed to parse
                logger.debug(f"[{job.hostname}/{job.app_name}] Found 0 files, 0 B")
                file_info = {
                    'files': [],
                    'total_size_bytes': 0,
                    'total_size_human': '0 B',
                    'file_count': 0,
                    'raw_output': output,
                    'error': None,
                    'ssh_error': False
                }
                results.append({
                    'job': job,
                    'exists': True,  # Path exists but no files
                    'file_info': file_info
                })

        return results

    async def _check_remote_file_exists_single(self, job: RsyncJob, attempt: int) -> Tuple[bool, Dict]:
        """
        Single attempt to check if remote files exist

        Args:
            job: RsyncJob to check
            attempt: Current attempt number (for logging)

        Returns:
            Tuple of (exists, file_info_dict)
        """
        ssh_target = f"{job.ssh_user}@{job.hostname}"

        # Use SSH to check if files exist
        cmd = [
            'ssh',
            '-p', str(job.ssh_port)
        ]

        # Add host key checking options if configured
        if job.ssh_ignore_host_key:
            cmd.extend([
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'UserKnownHostsFile=/dev/null',
                '-o', 'LogLevel=ERROR'  # Suppress SSH warnings
            ])

        # Add gateway/proxy jump host configuration if specified
        if job.gateway_host:
            gateway_user = job.gateway_user or job.ssh_user
            cmd.extend([
                '-o', f'ProxyJump={gateway_user}@{job.gateway_host}:{job.gateway_port}'
            ])
            logger.debug(
                f"[{job.hostname}/{job.app_name}] Using gateway for explore: {gateway_user}@{job.gateway_host}:{job.gateway_port}")

        # Use find command to recursively get all files with sizes
        # This handles directories that contain subdirectories with actual files
        find_cmd = f'find {job.remote_path} -type f -exec ls -la {{}} \\; 2>/dev/null || ls -la {job.remote_path} 2>&1'
        cmd.extend([
            ssh_target,
            find_cmd
        ])

        logger.debug(
            f"[{job.hostname}/{job.app_name}] Checking remote path: {job.remote_path} (attempt {attempt})"
        )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=self.timeout
        )

        stdout_str = stdout.decode('utf-8', errors='replace')
        stderr_str = stderr.decode('utf-8', errors='replace')

        if process.returncode == 0:
            # Parse the ls output to extract file information
            files = parse_ls_output(stdout_str)

            # Calculate totals
            total_size_bytes = sum(f['size_bytes']
                                   for f in files if not f['is_directory'])
            file_count = len([f for f in files if not f['is_directory']])

            logger.debug(
                f"[{job.hostname}/{job.app_name}] Found {file_count} files, {human_readable_size(total_size_bytes)}"
            )

            file_info = {
                'files': files,
                'total_size_bytes': total_size_bytes,
                'total_size_human': human_readable_size(total_size_bytes),
                'file_count': file_count,
                'raw_output': stdout_str,
                'error': None,
                'ssh_error': False
            }

            return True, file_info
        else:
            # Check if this looks like an SSH connection error vs file not found
            is_ssh_error = any(indicator in stderr_str.lower() for indicator in [
                'connection refused', 'connection timed out', 'host key verification failed',
                'permission denied', 'no route to host', 'connection reset',
                'ssh: could not resolve hostname', 'operation timed out'
            ])

            if is_ssh_error:
                # This is an SSH connection issue, not a file not found issue
                raise ConnectionError(
                    f"SSH connection issue: {stderr_str.strip()}")

            # This appears to be a legitimate "file not found" case
            logger.debug(
                f"[{job.hostname}/{job.app_name}] Path not found or no files: {job.remote_path}"
            )

            file_info = {
                'files': [],
                'total_size_bytes': 0,
                'total_size_human': '0 B',
                'file_count': 0,
                'raw_output': stderr_str,
                'error': stderr_str,
                'ssh_error': False
            }
            return False, file_info

    async def execute_jobs(self, jobs: List[RsyncJob], dry_run: bool = False) -> List[JobResult]:
        """
        Execute multiple rsync jobs in parallel

        Args:
            jobs: List of RsyncJobs to execute
            dry_run: If True, perform dry-run for all jobs

        Returns:
            List of JobResults
        """
        logger.info(f"Executing {len(jobs)} rsync jobs with max {self.max_parallel_jobs} parallel")
        
        # Log host summary like in explore mode
        host_summary = {}
        for job in jobs:
            if job.hostname not in host_summary:
                host_summary[job.hostname] = {'apps': set(), 'job_count': 0}
            host_summary[job.hostname]['apps'].add(job.app_name)
            host_summary[job.hostname]['job_count'] += 1
        
        logger.info(f"Syncing from {len(host_summary)} hosts:")
        for hostname, info in sorted(host_summary.items()):
            apps_str = ', '.join(sorted(info['apps']))
            logger.debug(f"Host {hostname}: {info['job_count']} jobs ({apps_str})")
        
        # Note: Unlike explore mode, sync mode cannot easily batch connections
        # because each rsync job transfers different files and needs its own connection
        logger.debug("Note: Each rsync job requires its own SSH connection")

        semaphore = asyncio.Semaphore(self.max_parallel_jobs)

        async def bounded_execute(job: RsyncJob) -> JobResult:
            async with semaphore:
                return await self.execute_job(job, dry_run)

        # Execute all jobs with bounded parallelism
        tasks = [bounded_execute(job) for job in jobs]
        results = await asyncio.gather(*tasks)

        # Log connection summary
        successful_jobs = sum(1 for r in results if r.success)
        failed_jobs = len(results) - successful_jobs
        
        if failed_jobs > 0:
            logger.warning(f"Sync completed: {successful_jobs}/{len(results)} jobs successful")
            # Log failed hosts
            failed_hosts = set()
            for result in results:
                if not result.success:
                    failed_hosts.add(result.job.hostname)
            if failed_hosts:
                logger.warning(f"Failed hosts: {', '.join(sorted(failed_hosts))}")
        else:
            logger.info(f"All {len(results)} sync jobs completed successfully")

        self.results.extend(results)
        return results

    async def explore_jobs(self, jobs: List[RsyncJob]) -> Dict[str, Dict]:
        """
        Explore remote files (check existence) for all jobs
        Optimized to batch multiple checks per host into a single SSH connection

        Args:
            jobs: List of RsyncJobs to explore

        Returns:
            Dictionary with exploration results
        """
        logger.info(f"Exploring {len(jobs)} remote locations")

        # Group jobs by hostname to minimize SSH connections
        jobs_by_host = {}
        for job in jobs:
            host_key = (job.hostname, job.ssh_user, job.ssh_port, job.gateway_host, job.gateway_user, job.gateway_port)
            if host_key not in jobs_by_host:
                jobs_by_host[host_key] = []
            jobs_by_host[host_key].append(job)

        logger.info(f"Batched into {len(jobs_by_host)} SSH connections (was {len(jobs)})")
        
        # Log the batching details
        for host_key, host_jobs in jobs_by_host.items():
            hostname = host_key[0]
            logger.debug(f"Host {hostname}: {len(host_jobs)} applications to check in single SSH connection")

        semaphore = asyncio.Semaphore(self.max_parallel_jobs)
        ssh_failures = []  # Track SSH connection failures

        async def bounded_explore_host(host_key, host_jobs):
            async with semaphore:
                return await self._check_remote_files_batched(host_jobs)

        # Create tasks for each host
        tasks = [bounded_explore_host(host_key, host_jobs) for host_key, host_jobs in jobs_by_host.items()]
        host_results = await asyncio.gather(*tasks)

        # Organize results by hostname and app
        organized = {}
        for host_result in host_results:
            for result in host_result:
                job = result['job']
                if job.hostname not in organized:
                    organized[job.hostname] = {}

                file_info = result['file_info']
                
                # Track SSH failures for summary
                if not result['exists'] and file_info.get('ssh_error', False):
                    ssh_failures.append({
                        'hostname': job.hostname,
                        'app_name': job.app_name,
                        'remote_path': job.remote_path,
                        'error': file_info.get('error', 'Unknown SSH error')
                    })

                organized[job.hostname][job.app_name] = {
                    'remote_path': job.remote_path,
                    'exists': result['exists'],
                    'files': file_info.get('files', []),
                    'total_size_bytes': file_info.get('total_size_bytes', 0),
                    'total_size_human': file_info.get('total_size_human', '0 B'),
                    'file_count': file_info.get('file_count', 0),
                    # Keep for backward compatibility
                    'output': file_info.get('raw_output', ''),
                    'error': file_info.get('error'),
                    'ssh_error': file_info.get('ssh_error', False)
                }

        # Log SSH failure summary
        if ssh_failures:
            logger.warning(
                f"SSH connection failures occurred during exploration:")
            for failure in ssh_failures:
                logger.warning(
                    f"  {failure['hostname']}/{failure['app_name']}: {failure['error']}"
                )
            logger.warning(
                f"Total SSH failures: {len(ssh_failures)}/{len(jobs)} checks"
            )
        else:
            logger.info("All SSH connections successful")

        return organized

    def get_summary(self) -> Dict[str, int]:
        """
        Get summary of job execution results

        Returns:
            Dictionary with success/failure counts
        """
        total = len(self.results)
        successful = sum(1 for r in self.results if r.success)
        failed = total - successful

        return {
            'total': total,
            'successful': successful,
            'failed': failed
        }

    def write_failure_log(self, log_path: Path = Path("logs/failures.log")):
        """
        Write failed jobs to a log file

        Args:
            log_path: Path to write failure log
        """
        log_path.parent.mkdir(parents=True, exist_ok=True)

        failed_results = [r for r in self.results if not r.success]

        if not failed_results:
            logger.info("No failures to log")
            return

        with open(log_path, 'a') as f:
            f.write(f"\n{'='*80}\n")
            f.write(
                f"Log collection failures - {datetime.now().isoformat()}\n")
            f.write(f"{'='*80}\n\n")

            for result in failed_results:
                f.write(f"Host: {result.job.hostname}\n")
                f.write(f"Application: {result.job.app_name}\n")
                f.write(f"Remote path: {result.job.remote_path}\n")
                f.write(f"Attempts: {result.attempts}\n")
                f.write(f"Return code: {result.return_code}\n")
                f.write(f"STDERR:\n{result.stderr}\n")
                f.write(f"{'-'*80}\n\n")

        logger.info(f"Failure log written to {log_path}")


if __name__ == "__main__":
    # Example usage
    async def test():
        manager = RsyncManager(max_parallel_jobs=2)

        jobs = [
            RsyncJob(
                hostname="example.com",
                app_name="nginx",
                remote_path="/var/log/nginx/*.log",
                local_path=Path("logs/example.com/nginx")
            )
        ]

        results = await manager.execute_jobs(jobs, dry_run=True)

        summary = manager.get_summary()
        print(f"Summary: {summary}")
