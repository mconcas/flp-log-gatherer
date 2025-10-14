"""
Rsync job manager for parallel log collection
"""
import subprocess
import asyncio
import logging
import re
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass


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
        
        # Add gateway/proxy jump host configuration if specified
        if job.gateway_host:
            gateway_user = job.gateway_user or job.ssh_user
            ssh_opts += f" -o ProxyJump={gateway_user}@{job.gateway_host}:{job.gateway_port}"
            logger.debug(f"[{job.hostname}/{job.app_name}] Using gateway: {gateway_user}@{job.gateway_host}:{job.gateway_port}")
        
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
        start_time = datetime.now()
        attempts = 0

        while attempts < self.retry_count:
            attempts += 1

            try:
                cmd = self.build_rsync_command(job, dry_run)
                logger.debug(
                    f"[{job.hostname}/{job.app_name}] Executing (attempt {attempts}/{self.retry_count}): {' '.join(cmd)}")

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
                    logger.debug(
                        f"[{job.hostname}/{job.app_name}] Success in {duration:.2f}s")
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
                    logger.debug(
                        f"[{job.hostname}/{job.app_name}] Failed with return code {process.returncode}")
                    if attempts < self.retry_count:
                        logger.debug(
                            f"[{job.hostname}/{job.app_name}] Retrying in {self.retry_delay}s...")
                        await asyncio.sleep(self.retry_delay)
                        continue
                    else:
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
                logger.error(f"[{job.hostname}/{job.app_name}] Exception: {e}")
                if attempts < self.retry_count:
                    logger.info(
                        f"[{job.hostname}/{job.app_name}] Retrying in {self.retry_delay}s...")
                    await asyncio.sleep(self.retry_delay)
                    continue
                else:
                    duration = (datetime.now() - start_time).total_seconds()
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
            logger.debug(f"[{job.hostname}/{job.app_name}] Using gateway for explore: {gateway_user}@{job.gateway_host}:{job.gateway_port}")

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
                raise ConnectionError(f"SSH connection issue: {stderr_str.strip()}")
            
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
        logger.info(
            f"Executing {len(jobs)} jobs with max {self.max_parallel_jobs} parallel")

        semaphore = asyncio.Semaphore(self.max_parallel_jobs)

        async def bounded_execute(job: RsyncJob) -> JobResult:
            async with semaphore:
                return await self.execute_job(job, dry_run)

        # Execute all jobs with bounded parallelism
        tasks = [bounded_execute(job) for job in jobs]
        results = await asyncio.gather(*tasks)

        self.results.extend(results)
        return results

    async def explore_jobs(self, jobs: List[RsyncJob]) -> Dict[str, Dict]:
        """
        Explore remote files (check existence) for all jobs

        Args:
            jobs: List of RsyncJobs to explore

        Returns:
            Dictionary with exploration results
        """
        logger.info(f"Exploring {len(jobs)} remote locations")

        semaphore = asyncio.Semaphore(self.max_parallel_jobs)
        ssh_failures = []  # Track SSH connection failures

        async def bounded_explore(job: RsyncJob):
            async with semaphore:
                exists, file_info = await self.check_remote_file_exists(job)
                
                # Track SSH failures for summary
                if not exists and file_info.get('ssh_error', False):
                    ssh_failures.append({
                        'hostname': job.hostname,
                        'app_name': job.app_name,
                        'remote_path': job.remote_path,
                        'error': file_info.get('error', 'Unknown SSH error')
                    })
                
                return {
                    'job': job,
                    'exists': exists,
                    'file_info': file_info
                }

        tasks = [bounded_explore(job) for job in jobs]
        results = await asyncio.gather(*tasks)

        # Organize results by hostname and app
        organized = {}
        for result in results:
            job = result['job']
            if job.hostname not in organized:
                organized[job.hostname] = {}

            file_info = result['file_info']
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
            logger.warning(f"SSH connection failures occurred during exploration:")
            for failure in ssh_failures:
                logger.warning(
                    f"  {failure['hostname']}/{failure['app_name']}: {failure['error']}"
                )
            logger.warning(
                f"Total SSH failures: {len(ssh_failures)}/{len(jobs)} connections"
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
