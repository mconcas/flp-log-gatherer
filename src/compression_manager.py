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
    
    def create_incremental_archive(self, hostname: str, force: bool = False) -> tuple[Path, int]:
        """
        Create incremental archive with only new files
        
        Args:
            hostname: Name of the host
            force: If True, archive all files regardless of tracking
            
        Returns:
            Tuple of (archive_path, number_of_files_added)
        """
        if force:
            logger.info(f"Creating full archive for {hostname} (force=True)")
            node_dir = self.base_path / hostname
            
            if not node_dir.exists():
                logger.warning(f"Node directory does not exist: {node_dir}")
                return None, 0
            
            # Get all files
            all_files = []
            for root, dirs, files in os.walk(node_dir):
                for file in files:
                    file_path = Path(root) / file
                    all_files.append(file_path)
            
            files_to_archive = all_files
        else:
            # Get only new files
            files_to_archive = self.get_new_files(hostname)
        
        if not files_to_archive:
            logger.info(f"No new files to archive for {hostname}")
            return None, 0
        
        logger.info(f"Creating archive for {hostname} with {len(files_to_archive)} files")
        
        # Create archive
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = self.get_archive_path(hostname, timestamp)
        
        with tarfile.open(archive_path, "w:gz") as tar:
            for file_path in files_to_archive:
                # Add file with path relative to base directory
                arcname = file_path.relative_to(self.base_path)
                try:
                    tar.add(file_path, arcname=arcname)
                    logger.debug(f"Added to archive: {arcname}")
                except Exception as e:
                    logger.error(f"Failed to add {file_path} to archive: {e}")
        
        # Update tracked files
        tracked = self.load_tracked_files(hostname)
        for file_path in files_to_archive:
            rel_path = str(file_path.relative_to(self.base_path))
            tracked.add(rel_path)
        self.save_tracked_files(hostname, tracked)
        
        # Get archive size
        archive_size = archive_path.stat().st_size
        size_mb = archive_size / (1024 * 1024)
        
        logger.info(f"Archive created: {archive_path} ({size_mb:.2f} MB, {len(files_to_archive)} files)")
        
        return archive_path, len(files_to_archive)
    
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
                archive_path, file_count = self.create_incremental_archive(hostname, force=force)
                
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
                print(f"    - {archive['name']} ({archive['size_mb']:.2f} MB) - {archive['created'].strftime('%Y-%m-%d %H:%M:%S')}")
            
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
