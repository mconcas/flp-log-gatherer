"""
Log collector orchestrator - coordinates inventory, config, and rsync operations
"""
import asyncio
import logging
from typing import List, Dict, Set
from pathlib import Path

from .inventory_parser import InventoryParser
from .config_manager import ConfigManager
from .rsync_manager import RsyncManager, RsyncJob

logger = logging.getLogger(__name__)


class LogCollector:
    """Main orchestrator for log collection"""
    
    def __init__(self, inventory_path: str, config_path: str):
        """
        Initialize the log collector
        
        Args:
            inventory_path: Path to Ansible inventory file
            config_path: Path to configuration YAML file
        """
        self.inventory = InventoryParser(inventory_path)
        self.config = ConfigManager(config_path)
        self.rsync_manager = None
        self.jobs: List[RsyncJob] = []
        
    def initialize(self) -> None:
        """Initialize all components and validate configuration"""
        logger.info("Initializing log collector...")
        
        # Parse inventory
        logger.info(f"Parsing inventory: {self.inventory.inventory_path}")
        self.inventory.parse()
        hosts = self.inventory.get_all_hosts()
        logger.info(f"Found {len(hosts)} hosts in {len(self.inventory.get_groups())} groups")
        
        # Load configuration
        logger.info(f"Loading configuration: {self.config.config_path}")
        self.config.load()
        
        # Validate configuration
        errors = self.config.validate()
        if errors:
            logger.error("Configuration validation failed:")
            for error in errors:
                logger.error(f"  - {error}")
            raise ValueError("Invalid configuration")
        
        # Initialize rsync manager with config options
        self.rsync_manager = RsyncManager(
            max_parallel_jobs=self.config.get_rsync_option('max_parallel_jobs', 5),
            retry_count=self.config.get_rsync_option('retry_count', 3),
            retry_delay=self.config.get_rsync_option('retry_delay', 5),
            timeout=self.config.get_rsync_option('timeout', 300)
        )
        
        logger.info("Initialization complete")
    
    def build_jobs(self) -> List[RsyncJob]:
        """
        Build rsync jobs based on inventory and configuration
        
        Returns:
            List of RsyncJob objects
        """
        jobs = []
        
        # Get all hosts
        all_hosts = self.inventory.get_all_hosts()
        
        logger.info(f"Building jobs for {len(all_hosts)} hosts...")
        
        for hostname in all_hosts:
            # Get groups this host belongs to
            groups = self.inventory.get_groups_for_host(hostname)
            
            # Collect all applications for this host (from all its groups)
            applications: Set[str] = set()
            
            # First, add applications from the special '_all_nodes' group
            all_nodes_apps = self.config.get_applications_for_group('_all_nodes')
            if all_nodes_apps:
                applications.update(all_nodes_apps)
                logger.debug(f"Added {len(all_nodes_apps)} applications from _all_nodes for {hostname}")
            
            # Then add applications from host's specific groups
            for group in groups:
                apps = self.config.get_applications_for_group(group)
                applications.update(apps)
            
            if not applications:
                logger.warning(f"No applications configured for host {hostname} (groups: {groups})")
                continue
            
            logger.info(f"Host {hostname}: {len(applications)} applications ({', '.join(applications)})")
            
            # Create jobs for each application
            for app_name in applications:
                log_paths = self.config.get_log_paths_for_application(app_name)
                
                if not log_paths:
                    logger.warning(f"No log paths configured for application {app_name}")
                    continue
                
                # Create a job for each log path
                for log_path in log_paths:
                    local_path = self.config.get_app_storage_path(hostname, app_name)
                    
                    job = RsyncJob(
                        hostname=hostname,
                        app_name=app_name,
                        remote_path=log_path,
                        local_path=local_path,
                        ssh_user=self.config.get_rsync_option('ssh_user', 'root'),
                        ssh_port=self.config.get_rsync_option('ssh_port', 22),
                        flags=self.config.get_rsync_base_flags()
                    )
                    
                    jobs.append(job)
        
        logger.info(f"Built {len(jobs)} rsync jobs")
        self.jobs = jobs
        return jobs
    
    async def collect_logs(self, dry_run: bool = False) -> Dict:
        """
        Collect logs from all configured hosts
        
        Args:
            dry_run: If True, perform a dry-run without actually copying files
            
        Returns:
            Dictionary with collection results
        """
        if not self.jobs:
            self.build_jobs()
        
        if not self.jobs:
            logger.warning("No jobs to execute")
            return {'total': 0, 'successful': 0, 'failed': 0}
        
        mode = "DRY-RUN" if dry_run else "SYNC"
        logger.info(f"Starting log collection ({mode}) for {len(self.jobs)} jobs...")
        
        # Execute all jobs
        results = await self.rsync_manager.execute_jobs(self.jobs, dry_run=dry_run)
        
        # Get summary
        summary = self.rsync_manager.get_summary()
        
        logger.info(f"Collection complete: {summary['successful']}/{summary['total']} successful")
        
        if summary['failed'] > 0:
            logger.warning(f"{summary['failed']} jobs failed")
            self.rsync_manager.write_failure_log()
        
        return summary
    
    async def explore_remote_files(self) -> Dict:
        """
        Explore remote files without copying (check if they exist)
        
        Returns:
            Dictionary with exploration results organized by host and application
        """
        if not self.jobs:
            self.build_jobs()
        
        if not self.jobs:
            logger.warning("No jobs to explore")
            return {}
        
        logger.info(f"Exploring remote files for {len(self.jobs)} jobs...")
        
        # Explore all remote locations
        results = await self.rsync_manager.explore_jobs(self.jobs)
        
        return results
    
    def print_exploration_results(self, results: Dict) -> None:
        """
        Print exploration results in a readable format
        
        Args:
            results: Exploration results from explore_remote_files()
        """
        print("\n" + "="*80)
        print("REMOTE FILE EXPLORATION RESULTS")
        print("="*80 + "\n")
        
        for hostname in sorted(results.keys()):
            print(f"Host: {hostname}")
            print("-" * 80)
            
            apps = results[hostname]
            for app_name in sorted(apps.keys()):
                app_info = apps[app_name]
                status = "✓ EXISTS" if app_info['exists'] else "✗ NOT FOUND"
                print(f"  [{app_name}] {status}")
                print(f"    Remote path: {app_info['remote_path']}")
                
                if app_info['exists']:
                    # Show first few lines of output
                    output_lines = app_info['output'].strip().split('\n')
                    if output_lines:
                        print(f"    Files found:")
                        for line in output_lines[:5]:  # Show first 5 files
                            print(f"      {line}")
                        if len(output_lines) > 5:
                            print(f"      ... and {len(output_lines) - 5} more")
                else:
                    error = app_info['output'].strip()
                    if error:
                        print(f"    Error: {error}")
                
                print()
            
            print()
    
    def print_summary(self) -> None:
        """Print summary of configured hosts and applications"""
        print("\n" + "="*80)
        print("LOG COLLECTION CONFIGURATION SUMMARY")
        print("="*80 + "\n")
        
        # Show applications applied to all nodes
        all_nodes_apps = self.config.get_applications_for_group('_all_nodes')
        if all_nodes_apps:
            print("Applications collected from ALL nodes:")
            for app in sorted(all_nodes_apps):
                print(f"  • {app}")
            print()
        
        all_hosts = self.inventory.get_all_hosts()
        print(f"Total hosts: {len(all_hosts)}\n")
        
        for hostname in sorted(all_hosts):
            groups = self.inventory.get_groups_for_host(hostname)
            
            # Collect applications
            applications: Set[str] = set()
            
            # Add _all_nodes applications
            if all_nodes_apps:
                applications.update(all_nodes_apps)
            
            # Add group-specific applications
            for group in groups:
                apps = self.config.get_applications_for_group(group)
                applications.update(apps)
            
            print(f"Host: {hostname}")
            print(f"  Groups: {', '.join(sorted(groups))}")
            print(f"  Applications: {', '.join(sorted(applications)) if applications else 'None'}")
            
            # Show log paths for each application
            for app_name in sorted(applications):
                log_paths = self.config.get_log_paths_for_application(app_name)
                print(f"    [{app_name}]")
                for path in log_paths:
                    print(f"      - {path}")
            
            print()


if __name__ == "__main__":
    # Example usage
    async def test():
        collector = LogCollector(
            inventory_path="config/hosts",
            config_path="config/config.yaml"
        )
        
        try:
            collector.initialize()
            collector.print_summary()
            
            # Dry run
            # summary = await collector.collect_logs(dry_run=True)
            # print(f"\nSummary: {summary}")
            
        except Exception as e:
            logger.error(f"Error: {e}")
    
    # asyncio.run(test())
