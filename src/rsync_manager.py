"""
Rsync job manager for parallel log collection
"""
import subprocess
import asyncio
import logging
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


@dataclass
class RsyncJob:
    """Represents a single rsync job"""
    hostname: str
    app_name: str
    remote_path: str
    local_path: Path
    ssh_user: str = "root"
    ssh_port: int = 22
    flags: List[str] = None
    
    def __post_init__(self):
        if self.flags is None:
            self.flags = ['-a', '--progress']


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
        
        # Add SSH options (port, etc.)
        ssh_opts = f"ssh -p {job.ssh_port}"
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
                logger.info(f"[{job.hostname}/{job.app_name}] Executing (attempt {attempts}/{self.retry_count}): {' '.join(cmd)}")
                
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
                    logger.error(f"[{job.hostname}/{job.app_name}] Timeout after {self.timeout}s")
                    if attempts < self.retry_count:
                        logger.info(f"[{job.hostname}/{job.app_name}] Retrying in {self.retry_delay}s...")
                        await asyncio.sleep(self.retry_delay)
                        continue
                    else:
                        duration = (datetime.now() - start_time).total_seconds()
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
                    logger.info(f"[{job.hostname}/{job.app_name}] Success in {duration:.2f}s")
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
                    logger.warning(f"[{job.hostname}/{job.app_name}] Failed with return code {process.returncode}")
                    if attempts < self.retry_count:
                        logger.info(f"[{job.hostname}/{job.app_name}] Retrying in {self.retry_delay}s...")
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
                    logger.info(f"[{job.hostname}/{job.app_name}] Retrying in {self.retry_delay}s...")
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
    
    async def check_remote_file_exists(self, job: RsyncJob) -> Tuple[bool, str]:
        """
        Check if remote files exist (explore mode)
        
        Args:
            job: RsyncJob to check
            
        Returns:
            Tuple of (exists, message)
        """
        ssh_target = f"{job.ssh_user}@{job.hostname}"
        
        # Use SSH to check if files exist
        cmd = [
            'ssh',
            '-p', str(job.ssh_port),
            ssh_target,
            f'ls -la {job.remote_path} 2>&1'
        ]
        
        try:
            logger.info(f"[{job.hostname}/{job.app_name}] Checking remote path: {job.remote_path}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30
            )
            
            stdout_str = stdout.decode('utf-8', errors='replace')
            stderr_str = stderr.decode('utf-8', errors='replace')
            
            if process.returncode == 0:
                return True, stdout_str
            else:
                return False, stderr_str
                
        except asyncio.TimeoutError:
            return False, "Timeout while checking remote files"
        except Exception as e:
            return False, str(e)
    
    async def execute_jobs(self, jobs: List[RsyncJob], dry_run: bool = False) -> List[JobResult]:
        """
        Execute multiple rsync jobs in parallel
        
        Args:
            jobs: List of RsyncJobs to execute
            dry_run: If True, perform dry-run for all jobs
            
        Returns:
            List of JobResults
        """
        logger.info(f"Executing {len(jobs)} jobs with max {self.max_parallel_jobs} parallel")
        
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
        
        async def bounded_explore(job: RsyncJob):
            async with semaphore:
                exists, output = await self.check_remote_file_exists(job)
                return {
                    'job': job,
                    'exists': exists,
                    'output': output
                }
        
        tasks = [bounded_explore(job) for job in jobs]
        results = await asyncio.gather(*tasks)
        
        # Organize results by hostname and app
        organized = {}
        for result in results:
            job = result['job']
            if job.hostname not in organized:
                organized[job.hostname] = {}
            organized[job.hostname][job.app_name] = {
                'remote_path': job.remote_path,
                'exists': result['exists'],
                'output': result['output']
            }
        
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
            f.write(f"Log collection failures - {datetime.now().isoformat()}\n")
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
    
    # Run test
    # asyncio.run(test())
