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
from .journal_collector import JournalCollector

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
        self.journal_collector = None
        self.jobs: List[RsyncJob] = []
        self.journal_tasks: List[Dict] = []

    def initialize(self) -> None:
        """Initialize all components and validate configuration"""
        logger.info("Initializing log collector...")

        # Parse inventory
        logger.info(f"Parsing inventory: {self.inventory.inventory_path}")
        self.inventory.parse()
        hosts = self.inventory.get_all_hosts()
        logger.info(
            f"Found {len(hosts)} hosts in {len(self.inventory.get_groups())} groups")

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
            max_parallel_jobs=self.config.get_rsync_option(
                'max_parallel_jobs', 5),
            retry_count=self.config.get_rsync_option('retry_count', 3),
            retry_delay=self.config.get_rsync_option('retry_delay', 5),
            timeout=self.config.get_rsync_option('timeout', 300)
        )

        # Initialize journal collector
        self.journal_collector = JournalCollector(
            ssh_user=self.config.get_rsync_option('ssh_user', 'root'),
            ssh_port=self.config.get_rsync_option('ssh_port', 22),
            ssh_ignore_host_key=self.config.get_rsync_option(
                'ssh_ignore_host_key', True),
            timeout=self.config.get_rsync_option('timeout', 300)
        )

        logger.info("Initialization complete")

    def build_jobs(self) -> List[RsyncJob]:
        """
        Build rsync jobs and journal collection tasks based on inventory and configuration

        Returns:
            List of RsyncJob objects
        """
        jobs = []
        journal_tasks = []

        # Get all hosts
        all_hosts = self.inventory.get_all_hosts()

        logger.info(f"Building jobs for {len(all_hosts)} hosts...")

        for hostname in all_hosts:
            # Get groups this host belongs to
            groups = self.inventory.get_groups_for_host(hostname)

            # Collect all applications for this host (from all its groups)
            applications: Set[str] = set()

            # First, add applications from the special '_all_nodes' group
            all_nodes_apps = self.config.get_applications_for_group(
                '_all_nodes')
            if all_nodes_apps:
                applications.update(all_nodes_apps)
                logger.debug(
                    f"Added {len(all_nodes_apps)} applications from _all_nodes for {hostname}")

            # Then add applications from host's specific groups
            for group in groups:
                apps = self.config.get_applications_for_group(group)
                applications.update(apps)

            if not applications:
                logger.warning(
                    f"No applications configured for host {hostname} (groups: {groups})")
                continue

            logger.debug(
                f"Host {hostname}: {len(applications)} applications ({', '.join(applications)})")

            # Create jobs for each application
            for app_name in applications:
                log_paths = self.config.get_log_paths_for_application(app_name)
                journal_enabled = self.config.is_journal_enabled(app_name)

                # Create rsync jobs for file-based logs
                if log_paths:
                    # Create a job for each log path
                    for log_path in log_paths:
                        local_path = self.config.get_app_storage_path(
                            hostname, app_name)

                        job = RsyncJob(
                            hostname=hostname,
                            app_name=app_name,
                            remote_path=log_path,
                            local_path=local_path,
                            ssh_user=self.config.get_rsync_option(
                                'ssh_user', 'root'),
                            ssh_port=self.config.get_rsync_option(
                                'ssh_port', 22),
                            ssh_ignore_host_key=self.config.get_rsync_option(
                                'ssh_ignore_host_key', True),
                            flags=self.config.get_rsync_base_flags()
                        )

                        jobs.append(job)

                # Create journal collection tasks
                if journal_enabled:
                    local_path = self.config.get_app_storage_path(
                        hostname, app_name)
                    journal_mode = self.config.get_journal_mode(app_name)

                    if journal_mode == 'binary':
                        # Binary mode: rsync journal files directly
                        journal_opts = self.config.get_journal_option(
                            'binary', {})
                        remote_journal_paths = journal_opts.get(
                            'remote_journal_path', '/var/log/journal/')

                        # Support both single path (string) and multiple paths (list)
                        if isinstance(remote_journal_paths, str):
                            remote_journal_paths = [remote_journal_paths]

                        # Create rsync job for each journal directory
                        for idx, remote_path in enumerate(remote_journal_paths):
                            # Use suffix for multiple paths to keep them separate
                            suffix = f"_{idx}" if len(
                                remote_journal_paths) > 1 else ""
                            subdir = f"journal{suffix}" if len(
                                remote_journal_paths) > 1 else "journal"

                            job = RsyncJob(
                                hostname=hostname,
                                app_name=f"{app_name}_journal{suffix}",
                                remote_path=remote_path,
                                local_path=local_path / subdir,
                                ssh_user=self.config.get_rsync_option(
                                    'ssh_user', 'root'),
                                ssh_port=self.config.get_rsync_option(
                                    'ssh_port', 22),
                                ssh_ignore_host_key=self.config.get_rsync_option(
                                    'ssh_ignore_host_key', True),
                                flags=self.config.get_rsync_base_flags()
                            )
                            jobs.append(job)
                    else:
                        # Export mode: use journalctl to export logs
                        task = {
                            'hostname': hostname,
                            'app_name': app_name,
                            'local_path': local_path,
                            'unit': self.journal_collector.get_unit_name_for_app(app_name),
                            'since_days': self.config.get_date_filter_days(),
                            'current_boot_only': True
                        }
                        journal_tasks.append(task)

                if not log_paths and not journal_enabled:
                    logger.warning(
                        f"No log paths or journal configured for application {app_name}")

        logger.info(
            f"Built {len(jobs)} rsync jobs"
            f"and {len(journal_tasks)} journal export tasks"
        )
        self.jobs = jobs
        self.journal_tasks = journal_tasks
        return jobs

    async def collect_logs(self, dry_run: bool = False) -> Dict:
        """
        Collect logs from all configured hosts (both file-based and journal)

        Args:
            dry_run: If True, perform a dry-run without actually copying files

        Returns:
            Dictionary with collection results
        """
        if not self.jobs and not self.journal_tasks:
            self.build_jobs()

        mode = "DRY-RUN" if dry_run else "SYNC"
        logger.info(
            f"Starting log collection ({mode}) - "
            f"{len(self.jobs)} rsync jobs, {len(self.journal_tasks)} journal tasks..."
        )

        # Execute rsync jobs
        rsync_summary = {'total': 0, 'successful': 0, 'failed': 0}
        if self.jobs:
            results = await self.rsync_manager.execute_jobs(self.jobs, dry_run=dry_run)
            rsync_summary = self.rsync_manager.get_summary()

            logger.info(
                f"Rsync complete: {rsync_summary['successful']}/{rsync_summary['total']} successful"
            )

            if rsync_summary['failed'] > 0:
                logger.warning(f"{rsync_summary['failed']} rsync jobs failed")
                self.rsync_manager.write_failure_log()

        # Execute journal collection tasks
        journal_summary = {'total': 0, 'successful': 0, 'failed': 0}
        if self.journal_tasks and not dry_run:
            logger.info(
                f"Collecting journals from {len(self.journal_tasks)} tasks...")

            journal_results = []
            for task in self.journal_tasks:
                result = await self.journal_collector.collect_journal(**task)
                journal_results.append(result)

            # Calculate journal summary
            journal_summary['total'] = len(journal_results)
            journal_summary['successful'] = sum(
                1 for r in journal_results if r['success'])
            journal_summary['failed'] = journal_summary['total'] - \
                journal_summary['successful']

            logger.info(
                f"Journal collection complete: "
                f"{journal_summary['successful']}/{journal_summary['total']} successful"
            )

            if journal_summary['failed'] > 0:
                logger.warning(
                    f"{journal_summary['failed']} journal tasks failed")

        # Combined summary
        combined_summary = {
            'total': rsync_summary['total'] + journal_summary['total'],
            'successful': rsync_summary['successful'] + journal_summary['successful'],
            'failed': rsync_summary['failed'] + journal_summary['failed'],
            'rsync': rsync_summary,
            'journal': journal_summary
        }

        logger.info(
            f"Overall collection complete: "
            f"{combined_summary['successful']}/{combined_summary['total']} successful"
        )

        return combined_summary

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

        total_size_all_hosts = 0
        total_files_all_hosts = 0

        for hostname in sorted(results.keys()):
            print(f"Host: \033[1;36m{hostname}\033[0m")
            print("-" * 80)

            host_total_size = 0
            host_total_files = 0

            apps = results[hostname]
            for app_name in sorted(apps.keys()):
                app_info = apps[app_name]
                # ANSI color codes: green for exists, red for not found
                if app_info['exists']:
                    status = "\033[92m✓ EXISTS\033[0m"  # Green
                else:
                    status = "\033[91m✗ NOT FOUND\033[0m"  # Red

                print(f"  [{app_name}] {status}")
                print(f"    Remote path: {app_info['remote_path']}")

                if app_info['exists']:
                    file_count = app_info.get('file_count', 0)
                    total_size_human = app_info.get('total_size_human', '0 B')
                    total_size_bytes = app_info.get('total_size_bytes', 0)

                    # Add to host totals
                    host_total_size += total_size_bytes
                    host_total_files += file_count

                    if file_count > 0:
                        print(
                            f"    \033[1mTotal: {file_count} files, {total_size_human}\033[0m")

                        # Show detailed file listing
                        files = app_info.get('files', [])
                        if files:
                            print(f"    Files found:")
                            # Sort files by size (largest first) for better visibility
                            sorted_files = sorted(files, key=lambda f: f.get(
                                'size_bytes', 0), reverse=True)

                            # Show up to 5 largest files
                            for file_info in sorted_files[:5]:
                                name = file_info.get('name', 'unknown')
                                size_human = file_info.get('size_human', '0 B')
                                mod_time = file_info.get('mod_time', '')
                                is_dir = file_info.get('is_directory', False)

                                if is_dir:
                                    print(
                                        f"      \033[94m{name}/\033[0m (directory)")
                                else:
                                    print(
                                        f"      {name} - {size_human} - {mod_time}")

                            if len(sorted_files) > 5:
                                remaining = len(sorted_files) - 5
                                print(f"      ... and {remaining} more files")
                    else:
                        print(f"    Path exists but no files found")
                else:
                    error = app_info.get('error') or app_info.get(
                        'output', '').strip()
                    if error:
                        # Filter out SSH warnings/noise
                        error_lines = []
                        for line in error.split('\n'):
                            line = line.strip()
                            # Skip SSH host key warnings
                            if 'Permanently added' in line and 'to the list of known hosts' in line:
                                continue
                            # Skip Warning: prefix if it was just about host keys
                            if line.startswith('Warning:') and 'Permanently added' in line:
                                continue
                            if line:
                                error_lines.append(line)

                        if error_lines:
                            print(f"    Error: {'; '.join(error_lines)}")

                print()

            # Show host summary if there are files
            if host_total_files > 0:
                from .rsync_manager import human_readable_size
                host_total_human = human_readable_size(host_total_size)
                print(
                    f"  \033[1;33mHost Total: {host_total_files} files, {host_total_human}\033[0m")
                total_size_all_hosts += host_total_size
                total_files_all_hosts += host_total_files

            print()

        # Show overall summary
        if total_files_all_hosts > 0:
            from .rsync_manager import human_readable_size
            total_size_human = human_readable_size(total_size_all_hosts)
            print("="*80)
            print(
                f"\033[1;32mOVERALL TOTAL: {total_files_all_hosts} files, {total_size_human}\033[0m")
            print("="*80)

        # Generate per-application summary in Markdown format
        self._print_application_summary_markdown(results)

    def _print_application_summary_markdown(self, results: Dict) -> None:
        """
        Print per-application summary in Markdown format for easy copy-pasting
        
        Args:
            results: Exploration results from explore_remote_files()
        """
        from .rsync_manager import human_readable_size
        
        # Aggregate data by application across all hosts
        app_summary = {}
        
        for hostname, apps in results.items():
            for app_name, app_info in apps.items():
                if app_info['exists']:
                    file_count = app_info.get('file_count', 0)
                    total_size_bytes = app_info.get('total_size_bytes', 0)
                    
                    if app_name not in app_summary:
                        app_summary[app_name] = {
                            'total_files': 0,
                            'total_size_bytes': 0,
                            'host_count': 0,
                            'hosts': []
                        }
                    
                    app_summary[app_name]['total_files'] += file_count
                    app_summary[app_name]['total_size_bytes'] += total_size_bytes
                    if file_count > 0:  # Only count hosts that actually have files
                        app_summary[app_name]['host_count'] += 1
                        app_summary[app_name]['hosts'].append(hostname)
        
        if not app_summary:
            return
            
        print("\n" + "="*80)
        print("APPLICATION SUMMARY (Markdown Format)")
        print("="*80)
        print("\n```markdown")
        print("# Log Collection Exploration Summary")
        print()
        print("## Per-Application Overview")
        print()
        print("| Application | Files Found | Total Size | Hosts with Files |")
        print("|-------------|-------------|------------|------------------|")
        
        # Calculate overall totals
        grand_total_files = 0
        grand_total_size = 0
        
        # Sort applications by total size (largest first)
        sorted_apps = sorted(app_summary.items(), 
                           key=lambda x: x[1]['total_size_bytes'], 
                           reverse=True)
        
        for app_name, summary in sorted_apps:
            files = summary['total_files']
            size_human = human_readable_size(summary['total_size_bytes'])
            host_count = summary['host_count']
            
            grand_total_files += files
            grand_total_size += summary['total_size_bytes']
            
            print(f"| `{app_name}` | {files:,} | {size_human} | {host_count} |")
        
        # Add totals row
        grand_total_human = human_readable_size(grand_total_size)
        print(f"| **TOTAL** | **{grand_total_files:,}** | **{grand_total_human}** | **{len(results)}** |")
        
        print()
        print("## Detailed Breakdown")
        print()
        
        for app_name, summary in sorted_apps:
            if summary['total_files'] > 0:
                files = summary['total_files']
                size_human = human_readable_size(summary['total_size_bytes'])
                hosts = summary['hosts']
                
                print(f"### {app_name}")
                print(f"- **Files:** {files:,}")
                print(f"- **Total Size:** {size_human}")
                print(f"- **Hosts:** {len(hosts)} host(s)")
                if len(hosts) <= 10:  # Show host list if not too many
                    print(f"- **Host List:** {', '.join(sorted(hosts))}")
                else:
                    print(f"- **Host List:** {', '.join(sorted(hosts[:10]))} + {len(hosts)-10} more")
                print()
        
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"*Generated on {timestamp}*")
        print("```")
        print("="*80)

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
            print(
                f"  Applications: {', '.join(sorted(applications)) if applications else 'None'}")

            # Show log paths and journal settings for each application
            for app_name in sorted(applications):
                log_paths = self.config.get_log_paths_for_application(app_name)
                journal_enabled = self.config.is_journal_enabled(app_name)

                print(f"    [{app_name}]")
                if log_paths:
                    print(f"      File-based logs:")
                    for path in log_paths:
                        print(f"        - {path}")
                if journal_enabled:
                    journal_mode = self.config.get_journal_mode(app_name)
                    if journal_mode == 'binary':
                        print(
                            f"      Journal: enabled (binary copy - minimal remote impact)")
                    else:
                        unit = self.journal_collector.get_unit_name_for_app(
                            app_name)
                        unit_str = f" (unit: {unit})" if unit else " (all units)"
                        print(
                            f"      Journal: enabled (export mode{unit_str})")
                if not log_paths and not journal_enabled:
                    print(f"      (no logs configured)")

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
