#!/usr/bin/env python3
"""
flp-log-gatherer - Main CLI application for collecting logs from heterogeneous nodes
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

from src.log_collector import LogCollector
from src.compression_manager import CompressionManager
from src.probe_manager import ProbeManager
from src.inventory_parser import InventoryParser


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colored output based on log level"""

    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[1;31m',  # Bold Red
    }
    RESET = '\033[0m'

    def format(self, record):
        # Add color to the level name
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = f"{self.COLORS[levelname]}{levelname}{self.RESET}"

        # Format the message
        result = super().format(record)

        return result


# Set up logging
def setup_logging(verbose: bool = False):
    """Configure logging based on verbosity level"""
    level = logging.DEBUG if verbose else logging.INFO

    # Create handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    # Create and set formatter
    formatter = ColoredFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)

    # Configure root logger
    logging.root.setLevel(level)
    logging.root.handlers = []
    logging.root.addHandler(handler)


async def run_sync(args):
    """Run log synchronization"""
    collector = LogCollector(
        inventory_path=args.inventory,
        config_path=args.config
    )

    try:
        # Initialize
        collector.initialize()

        # Ensure local storage directory exists
        storage_path = collector.config.get_local_storage_path()
        storage_path.mkdir(parents=True, exist_ok=True)
        logging.info(f"Using local storage: {storage_path}")

        # Show summary if requested
        if args.show_summary:
            collector.print_summary()
            return 0

        # Collect logs
        print(f"\nStarting log collection (dry-run: {args.dry_run})...")
        summary = await collector.collect_logs(dry_run=args.dry_run)

        print(f"\n{'='*80}")
        print("COLLECTION SUMMARY")
        print(f"{'='*80}")
        print(f"Total jobs: {summary['total']}")
        print(f"Successful: {summary['successful']}")
        print(f"Failed: {summary['failed']}")
        print(f"{'='*80}\n")

        # Compress if not dry-run and compression is enabled
        if not args.dry_run and args.compress:
            print("Compressing collected logs in parallel...")
            compression_manager = CompressionManager(
                base_path=collector.config.get_local_storage_path())
            compression_results = await compression_manager.compress_all_hosts_parallel()

            print(f"\n{'='*80}")
            print("COMPRESSION SUMMARY")
            print(f"{'='*80}")

            for hostname, result in compression_results.items():
                if result['success']:
                    if result['file_count'] > 0:
                        print(
                            f"✓ {hostname}: {result['file_count']} files archived")
                        print(f"  Archive: {result['archive_path']}")
                    else:
                        print(f"○ {hostname}: No new files to archive")
                else:
                    print(
                        f"✗ {hostname}: Failed - {result.get('error', 'Unknown error')}")

            print(f"{'='*80}\n")

        return 0 if summary['failed'] == 0 else 1

    except Exception as e:
        logging.error(f"Error during log collection: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


async def run_explore(args):
    """Run exploration mode to check remote files"""
    collector = LogCollector(
        inventory_path=args.inventory,
        config_path=args.config
    )

    try:
        # Initialize
        collector.initialize()

        # Explore remote files
        print("\nExploring remote files...")
        results = await collector.explore_remote_files()

        # Print results
        collector.print_exploration_results(results)

        # Save SUMMARY.md file
        collector._save_application_summary_markdown(results)

        return 0

    except Exception as e:
        logging.error(f"Error during exploration: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


async def run_probe(args):
    """Run probe to test connectivity and SSH access"""
    try:
        # Parse inventory to get hosts
        inventory = InventoryParser(args.inventory)
        inventory.parse()
        hosts = inventory.get_all_hosts()

        if not hosts:
            logging.error("No hosts found in inventory")
            return 1

        # Load config to get SSH settings
        from src.config_manager import ConfigManager
        config = ConfigManager(args.config)
        config.load()

        rsync_opts = config.config.get('rsync_options', {})
        ssh_user = rsync_opts.get('ssh_user', 'root')
        ssh_ignore_host_key = rsync_opts.get('ssh_ignore_host_key', True)
        # Invert the logic: if we ignore host keys, then strict checking is False
        strict_host_key = not ssh_ignore_host_key

        # Create probe manager
        probe_manager = ProbeManager(
            ssh_user=ssh_user,
            strict_host_key_checking=strict_host_key,
            gateway_host=config.get_gateway_host(),
            gateway_user=config.get_gateway_user() if config.is_gateway_enabled() else None,
            gateway_port=config.get_gateway_port(),
            retry_count=config.get_rsync_option('retry_count', 3),
            retry_delay=config.get_rsync_option('retry_delay', 2)
        )

        # Probe all hosts
        results = await probe_manager.probe_hosts(hosts)

        # Print results
        gateway_info = None
        if config.is_gateway_enabled():
            gateway_info = {
                'host': config.get_gateway_host(),
                'user': config.get_gateway_user(),
                'port': config.get_gateway_port()
            }
        probe_manager.print_probe_results(results, gateway_info)

        # Return success if all hosts are reachable
        all_ok = all(r['ping_success'] and r['ssh_success'] for r in results)
        return 0 if all_ok else 1

    except Exception as e:
        logging.error(f"Error during probe: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def run_list_archives(args):
    """List available archives"""
    # Load config to get storage path
    from src.config_manager import ConfigManager
    config = ConfigManager(args.config)
    config.load()

    compression_manager = CompressionManager(
        base_path=config.get_local_storage_path())

    if args.host:
        print(f"\nArchives for host: {args.host}")
        archives = compression_manager.list_archives(hostname=args.host)
    else:
        compression_manager.print_archive_summary()
        return 0

    if not archives:
        print("No archives found")
        return 0

    print(f"\n{'='*80}")
    for archive in archives:
        print(f"  {archive['name']}")
        print(f"    Size: {archive['size_mb']:.2f} MB")
        print(
            f"    Created: {archive['created'].strftime('%Y-%m-%d %H:%M:%S')}")
        print()

    return 0


async def run_compress(args):
    """Run compression on already-collected logs in parallel"""
    # Load config to get storage path
    from src.config_manager import ConfigManager
    config = ConfigManager(args.config)
    config.load()

    compression_manager = CompressionManager(
        base_path=config.get_local_storage_path())

    from datetime import datetime
    start_time = datetime.now()
    print("Compressing collected logs in parallel...")
    results = await compression_manager.compress_all_hosts_parallel(force=args.force)
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    print(f"\n{'='*80}")
    print("COMPRESSION SUMMARY")
    print(f"{'='*80}")

    total = len(results)
    successful = sum(1 for r in results.values() if r['success'])

    for hostname, result in results.items():
        if result['success']:
            if result['file_count'] > 0:
                print(f"✓ {hostname}: {result['file_count']} files archived")
                print(f"  Archive: {result['archive_path']}")
            else:
                print(f"○ {hostname}: No new files to archive")
        else:
            print(f"✗ {hostname}: Failed - {result.get('error', 'Unknown error')}")

    print(f"\nTotal: {successful}/{total} successful")
    print(f"Compression completed in {duration:.2f} seconds")
    print(f"{'='*80}\n")

    return 0 if successful == total else 1


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='flp-log-gatherer - Collect logs from heterogeneous nodes using rsync',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Normal sync (no compression)
  %(prog)s sync
  
  # Sync with automatic compression
  %(prog)s sync --compress
  
  # Dry-run to see what would be synced
  %(prog)s sync --dry-run
  
  # Explore remote files without syncing
  %(prog)s explore
  
  # Test connectivity and SSH access
  %(prog)s probe
  
  # Show configuration summary
  %(prog)s sync --show-summary
  
  # Compress already-collected logs
  %(prog)s compress
  
  # List all archives
  %(prog)s list-archives
        """
    )

    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable verbose output')
    parser.add_argument('-c', '--config', default='config/config.yaml',
                        help='Path to configuration file (default: config/config.yaml)')
    parser.add_argument('-i', '--inventory', default='config/hosts',
                        help='Path to Ansible inventory file (default: config/hosts)')

    subparsers = parser.add_subparsers(
        dest='command', help='Command to execute')

    # Sync command
    sync_parser = subparsers.add_parser(
        'sync', help='Synchronize logs from remote hosts')
    sync_parser.add_argument('--dry-run', action='store_true',
                             help='Perform a dry-run without actually copying files')
    sync_parser.add_argument('--compress', action='store_true',
                             help='Compress collected logs after syncing (creates tar.gz archives)')
    sync_parser.add_argument('--show-summary', action='store_true',
                             help='Show configuration summary and exit')

    # Explore command
    explore_parser = subparsers.add_parser('explore',
                                           help='Explore remote files without syncing')

    # Probe command
    probe_parser = subparsers.add_parser('probe',
                                         help='Test connectivity and SSH access to all hosts')

    # Compress command
    compress_parser = subparsers.add_parser('compress',
                                            help='Compress already-collected logs')
    compress_parser.add_argument('--force', action='store_true',
                                 help='Force re-compression of all files')

    # List archives command
    list_parser = subparsers.add_parser('list-archives',
                                        help='List available archives')
    list_parser.add_argument('--host', help='Filter archives by hostname')

    args = parser.parse_args()

    # Set up logging
    setup_logging(args.verbose)

    # Default to sync if no command specified
    if not args.command:
        args.command = 'sync'
        args.dry_run = False
        args.compress = False
        args.show_summary = False

    # Execute command
    try:
        if args.command == 'sync':
            return asyncio.run(run_sync(args))
        elif args.command == 'explore':
            return asyncio.run(run_explore(args))
        elif args.command == 'probe':
            return asyncio.run(run_probe(args))
        elif args.command == 'compress':
            return asyncio.run(run_compress(args))
        elif args.command == 'list-archives':
            return run_list_archives(args)
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        return 130


if __name__ == '__main__':
    sys.exit(main())
