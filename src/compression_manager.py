"""
Compression manager for incremental archiving of collected logs
"""
import tarfile
import gzip
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Set
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

logger = logging.getLogger(__name__)


class CompressionManager:
    """Manage compression of collected logs"""

    def __init__(self, base_path: Path = Path("logs")):
        """
        Initialize the compression manager

        Args:
            base_path: Base path where logs are collected
        """
        self.base_path = Path(base_path)
        self.archive_dir = self.base_path / "archives"
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def get_archive_path(self, hostname: str, timestamp: str = None) -> Path:
        """
        Get the archive file path for a host

        Args:
            hostname: Name of the host
            timestamp: Optional timestamp for archive name

        Returns:
            Path to archive file
        """
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        archive_name = f"{hostname}_{timestamp}.tar.gz"
        return self.archive_dir / archive_name

    def get_tracked_files_path(self, hostname: str) -> Path:
        """
        Get path to file tracking already-archived files

        Args:
            hostname: Name of the host

        Returns:
            Path to tracking file
        """
        return self.archive_dir / f".{hostname}_tracked.txt"
    
    def get_metadata_path(self, hostname: str) -> Path:
        """
        Get path to metadata file for tracking directory state

        Args:
            hostname: Name of the host

        Returns:
            Path to metadata file
        """
        return self.archive_dir / f".{hostname}_metadata.json"

    def load_tracked_files(self, hostname: str) -> Set[str]:
        """
        Load set of files already added to archives

        Args:
            hostname: Name of the host

        Returns:
            Set of file paths that have been archived
        """
        tracked_path = self.get_tracked_files_path(hostname)

        if not tracked_path.exists():
            return set()

        tracked = set()
        with open(tracked_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    tracked.add(line)

        return tracked

    def save_tracked_files(self, hostname: str, tracked: Set[str]) -> None:
        """
        Save set of tracked files

        Args:
            hostname: Name of the host
            tracked: Set of file paths to track
        """
        tracked_path = self.get_tracked_files_path(hostname)

        with open(tracked_path, 'w') as f:
            for file_path in sorted(tracked):
                f.write(f"{file_path}\n")

    def get_new_files(self, hostname: str) -> List[Path]:
        """
        Get list of new files that haven't been archived yet

        Args:
            hostname: Name of the host

        Returns:
            List of Path objects for new files
        """
        node_dir = self.base_path / hostname

        if not node_dir.exists():
            logger.warning(f"Node directory does not exist: {node_dir}")
            return []

        # Get all files in node directory and subdirectories
        all_files = []
        for root, dirs, files in os.walk(node_dir):
            for file in files:
                file_path = Path(root) / file
                all_files.append(file_path)

        # Load tracked files
        tracked = self.load_tracked_files(hostname)

        # Filter to only new files
        new_files = []
        for file_path in all_files:
            # Use relative path for tracking
            rel_path = str(file_path.relative_to(self.base_path))
            if rel_path not in tracked:
                new_files.append(file_path)

        return new_files

    def get_directory_state(self, hostname: str) -> dict:
        """
        Get current state of the directory (file paths, sizes, modification times)

        Args:
            hostname: Name of the host

        Returns:
            Dictionary with directory state information
        """
        node_dir = self.base_path / hostname
        
        if not node_dir.exists():
            return {}

        state = {
            'files': {},
            'total_size': 0,
            'file_count': 0,
            'latest_mtime': 0
        }

        for root, dirs, files in os.walk(node_dir):
            for file in files:
                file_path = Path(root) / file
                rel_path = str(file_path.relative_to(self.base_path))
                
                try:
                    stat = file_path.stat()
                    state['files'][rel_path] = {
                        'size': stat.st_size,
                        'mtime': stat.st_mtime
                    }
                    state['total_size'] += stat.st_size
                    state['file_count'] += 1
                    state['latest_mtime'] = max(state['latest_mtime'], stat.st_mtime)
                except (OSError, IOError) as e:
                    logger.warning(f"Could not stat file {file_path}: {e}")
                    continue

        return state

    def load_last_archive_metadata(self, hostname: str) -> dict:
        """
        Load metadata from the last archive creation

        Args:
            hostname: Name of the host

        Returns:
            Dictionary with last archive metadata
        """
        metadata_path = self.get_metadata_path(hostname)
        
        if not metadata_path.exists():
            return {}

        try:
            with open(metadata_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not load metadata for {hostname}: {e}")
            return {}

    def save_archive_metadata(self, hostname: str, state: dict, archive_path: Path) -> None:
        """
        Save metadata about the created archive

        Args:
            hostname: Name of the host
            state: Directory state that was archived
            archive_path: Path to the created archive
        """
        metadata = {
            'archive_path': str(archive_path),
            'creation_time': datetime.now().isoformat(),
            'directory_state': state
        }
        
        metadata_path = self.get_metadata_path(hostname)
        
        try:
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
        except IOError as e:
            logger.warning(f"Could not save metadata for {hostname}: {e}")

    def needs_new_archive(self, hostname: str, force: bool = False) -> tuple[bool, str]:
        """
        Check if a new archive is needed for the host

        Args:
            hostname: Name of the host
            force: If True, always return True

        Returns:
            Tuple of (needs_archive, reason)
        """
        if force:
            return True, "force flag set"

        # Get current directory state
        current_state = self.get_directory_state(hostname)
        
        if not current_state or current_state['file_count'] == 0:
            return False, "no files found"

        # Load last archive metadata
        last_metadata = self.load_last_archive_metadata(hostname)
        
        if not last_metadata:
            return True, "no previous archive metadata found"

        last_state = last_metadata.get('directory_state', {})
        
        # Compare file counts
        if current_state['file_count'] != last_state.get('file_count', 0):
            return True, f"file count changed: {last_state.get('file_count', 0)} -> {current_state['file_count']}"

        # Compare total size
        if current_state['total_size'] != last_state.get('total_size', 0):
            return True, f"total size changed: {last_state.get('total_size', 0)} -> {current_state['total_size']}"

        # Compare latest modification time
        if current_state['latest_mtime'] > last_state.get('latest_mtime', 0):
            return True, f"newer files detected (latest mtime: {datetime.fromtimestamp(current_state['latest_mtime'])})"

        # Check if any individual files changed
        current_files = current_state.get('files', {})
        last_files = last_state.get('files', {})
        
        for file_path, file_info in current_files.items():
            if file_path not in last_files:
                return True, f"new file detected: {file_path}"
            
            last_file_info = last_files[file_path]
            if (file_info['size'] != last_file_info.get('size', 0) or 
                file_info['mtime'] != last_file_info.get('mtime', 0)):
                return True, f"file changed: {file_path}"

        return False, "no changes detected"

    def get_existing_archive_path(self, hostname: str) -> Path:
        """
        Get path to the most recent existing archive for a host

        Args:
            hostname: Name of the host

        Returns:
            Path to the existing archive, or None if not found
        """
        last_metadata = self.load_last_archive_metadata(hostname)
        existing_archive = last_metadata.get('archive_path')
        
        if existing_archive and Path(existing_archive).exists():
            return Path(existing_archive)
        
        # Fallback: search for the most recent archive file
        pattern = f"{hostname}_*.tar.gz"
        archives = list(self.archive_dir.glob(pattern))
        if archives:
            # Sort by modification time, newest first
            archives.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return archives[0]
        
        return None

    def check_compression_status(self, hostname: str) -> dict:
        """
        Check compression status for a host without actually compressing

        Args:
            hostname: Name of the host

        Returns:
            Dictionary with status information
        """
        needs_archive, reason = self.needs_new_archive(hostname, force=False)
        current_state = self.get_directory_state(hostname)
        existing_archive = self.get_existing_archive_path(hostname)
        
        status = {
            'hostname': hostname,
            'needs_new_archive': needs_archive,
            'reason': reason,
            'file_count': current_state.get('file_count', 0),
            'total_size': current_state.get('total_size', 0),
            'existing_archive': str(existing_archive) if existing_archive else None
        }
        
        if existing_archive and existing_archive.exists():
            status['existing_archive_size'] = existing_archive.stat().st_size
            
        return status

    def create_incremental_archive(self, hostname: str, force: bool = False) -> tuple[Path, int]:
        """
        Create incremental archive with only new files (idempotent)

        Args:
            hostname: Name of the host
            force: If True, archive all files regardless of tracking

        Returns:
            Tuple of (archive_path, number_of_files_added)
        """
        # Check if new archive is needed
        needs_archive, reason = self.needs_new_archive(hostname, force)
        
        if not needs_archive:
            logger.info(f"No new archive needed for {hostname}: {reason}")
            # Return the existing archive path if it exists
            existing_archive = self.get_existing_archive_path(hostname)
            return existing_archive, 0

        logger.info(f"Creating new archive for {hostname}: {reason}")
        
        # Get current directory state
        current_state = self.get_directory_state(hostname)
        
        if not current_state or current_state['file_count'] == 0:
            logger.warning(f"No files found for {hostname}")
            return None, 0

        node_dir = self.base_path / hostname
        
        # Get all files (we always create full archives for idempotency)
        all_files = []
        for root, dirs, files in os.walk(node_dir):
            for file in files:
                file_path = Path(root) / file
                all_files.append(file_path)

        if not all_files:
            logger.info(f"No files to archive for {hostname}")
            return None, 0

        logger.info(f"Creating archive for {hostname} with {len(all_files)} files")

        # Create archive with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = self.get_archive_path(hostname, timestamp)

        with tarfile.open(archive_path, "w:gz") as tar:
            for file_path in all_files:
                # Add file with path relative to base directory
                arcname = file_path.relative_to(self.base_path)
                try:
                    tar.add(file_path, arcname=arcname)
                    logger.debug(f"Added to archive: {arcname}")
                except Exception as e:
                    logger.error(f"Failed to add {file_path} to archive: {e}")

        # Save metadata about this archive
        self.save_archive_metadata(hostname, current_state, archive_path)

        # Update tracked files (for backward compatibility)
        tracked = set()
        for file_path in all_files:
            rel_path = str(file_path.relative_to(self.base_path))
            tracked.add(rel_path)
        self.save_tracked_files(hostname, tracked)

        # Get archive size
        archive_size = archive_path.stat().st_size
        size_mb = archive_size / (1024 * 1024)

        logger.info(f"Archive created: {archive_path} ({size_mb:.2f} MB, {len(all_files)} files)")

        return archive_path, len(all_files)

    def compress_all_hosts(self, force: bool = False) -> dict:
        """
        Create archives for all hosts that have collected logs

        Args:
            force: If True, archive all files regardless of tracking

        Returns:
            Dictionary with compression results per host
        """
        results = {}

        # Find all host directories
        if not self.base_path.exists():
            logger.warning(f"Base path does not exist: {self.base_path}")
            return results

        host_dirs = [d for d in self.base_path.iterdir()
                     if d.is_dir() and d.name != "archives"]

        logger.info(f"Compressing logs for {len(host_dirs)} hosts...")

        for host_dir in host_dirs:
            hostname = host_dir.name

            try:
                archive_path, file_count = self.create_incremental_archive(
                    hostname, force=force)

                results[hostname] = {
                    'success': True,
                    'archive_path': str(archive_path) if archive_path else None,
                    'file_count': file_count
                }

            except Exception as e:
                logger.error(f"Failed to compress logs for {hostname}: {e}")
                results[hostname] = {
                    'success': False,
                    'error': str(e),
                    'file_count': 0
                }

        return results

    async def compress_all_hosts_parallel(self, force: bool = False, max_workers: int = None) -> dict:
        """
        Create archives for all hosts in parallel using thread pool

        Args:
            force: If True, archive all files regardless of tracking
            max_workers: Maximum number of parallel compression threads (defaults to CPU count)

        Returns:
            Dictionary with compression results per host
        """
        results = {}

        # Find all host directories
        if not self.base_path.exists():
            logger.warning(f"Base path does not exist: {self.base_path}")
            return results

        host_dirs = [d for d in self.base_path.iterdir()
                     if d.is_dir() and d.name != "archives"]

        if not host_dirs:
            logger.info("No host directories found for compression")
            return results

        # Determine optimal number of workers
        if max_workers is None:
            max_workers = min(len(host_dirs), os.cpu_count() or 1)

        logger.info(f"Compressing logs for {len(host_dirs)} hosts in parallel (max {max_workers} workers)...")
        print(f"DEBUG: Using parallel compression with {max_workers} workers for {len(host_dirs)} hosts")

        # Create compression tasks
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all compression tasks
            tasks = {}
            for host_dir in host_dirs:
                hostname = host_dir.name
                future = loop.run_in_executor(
                    executor, 
                    self._compress_host_sync, 
                    hostname, 
                    force
                )
                tasks[hostname] = future

            # Wait for all tasks to complete
            for hostname, task in tasks.items():
                try:
                    result = await task
                    results[hostname] = result
                except Exception as e:
                    logger.error(f"Failed to compress logs for {hostname}: {e}")
                    results[hostname] = {
                        'success': False,
                        'error': str(e),
                        'file_count': 0
                    }

        return results

    def _compress_host_sync(self, hostname: str, force: bool = False) -> dict:
        """
        Synchronous compression method for use in thread pool
        
        Args:
            hostname: Name of the host to compress
            force: If True, archive all files regardless of tracking
            
        Returns:
            Dictionary with compression result for the host
        """
        try:
            archive_path, file_count = self.create_incremental_archive(
                hostname, force=force)

            return {
                'success': True,
                'archive_path': str(archive_path) if archive_path else None,
                'file_count': file_count
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
                'file_count': 0
            }

    def list_archives(self, hostname: str = None) -> List[dict]:
        """
        List available archives

        Args:
            hostname: Optional hostname to filter archives

        Returns:
            List of archive information dictionaries
        """
        archives = []

        pattern = f"{hostname}_*.tar.gz" if hostname else "*.tar.gz"

        for archive_path in self.archive_dir.glob(pattern):
            # Skip tracking files
            if archive_path.name.startswith('.'):
                continue

            stat = archive_path.stat()
            size_mb = stat.st_size / (1024 * 1024)

            archives.append({
                'path': archive_path,
                'name': archive_path.name,
                'size_mb': size_mb,
                'created': datetime.fromtimestamp(stat.st_mtime)
            })

        # Sort by creation time (newest first)
        archives.sort(key=lambda x: x['created'], reverse=True)

        return archives

    def print_archive_summary(self) -> None:
        """Print summary of all archives"""
        archives = self.list_archives()

        if not archives:
            print("No archives found")
            return

        print("\n" + "="*80)
        print("ARCHIVE SUMMARY")
        print("="*80 + "\n")

        # Group by hostname
        by_host = {}
        for archive in archives:
            hostname = archive['name'].split('_')[0]
            if hostname not in by_host:
                by_host[hostname] = []
            by_host[hostname].append(archive)

        for hostname in sorted(by_host.keys()):
            host_archives = by_host[hostname]
            total_size = sum(a['size_mb'] for a in host_archives)

            print(f"Host: {hostname}")
            print(f"  Archives: {len(host_archives)}")
            print(f"  Total size: {total_size:.2f} MB")

            for archive in host_archives[:3]:  # Show latest 3
                print(
                    f"    - {archive['name']} ({archive['size_mb']:.2f} MB) - {archive['created'].strftime('%Y-%m-%d %H:%M:%S')}")

            if len(host_archives) > 3:
                print(f"    ... and {len(host_archives) - 3} more")

            print()


if __name__ == "__main__":
    # Example usage
    manager = CompressionManager()

    # Compress all hosts
    # results = manager.compress_all_hosts()
    # print(f"Compression results: {results}")

    # List archives
    # manager.print_archive_summary()
