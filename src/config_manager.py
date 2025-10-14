"""
Configuration manager for flp-log-gatherer
"""
import yaml
from typing import Dict, List, Any, Optional
from pathlib import Path
from datetime import datetime


class ConfigManager:
    """Manage configuration for log collection"""

    def __init__(self, config_path: str = "config/config.yaml"):
        """
        Initialize the configuration manager

        Args:
            config_path: Path to the YAML configuration file
        """
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
        self.applications: Dict[str, Dict[str, Any]] = {}
        self.node_groups: Dict[str, List[str]] = {}
        self.rsync_options: Dict[str, Any] = {}

    def load(self) -> None:
        """Load and parse the configuration file"""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self.config_path}")

        with open(self.config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        # Extract main sections
        self.applications = self.config.get('applications', {})
        self.node_groups = self.config.get('node_groups', {})
        self.rsync_options = self.config.get('rsync_options', {})

        # Set defaults
        self._set_defaults()

    def _set_defaults(self) -> None:
        """Set default values for missing configuration options"""
        defaults = {
            'max_parallel_jobs': 5,
            'compress': False,  # Disable post-sync compression by default
            'local_storage': 'logs',
            'ssh_user': 'root',
            'ssh_port': 22,
            'ssh_ignore_host_key': True,  # Ignore host key verification by default
            # Gateway/proxy configuration
            'gateway_host': None,  # SSH gateway/jump host (e.g., 'gateway.example.com')
            'gateway_user': None,  # SSH user for gateway (defaults to ssh_user if not set)
            'gateway_port': 22,    # SSH port for gateway
            # Disable rsync compression by default (reduces remote CPU load)
            'use_compression': False,
            'additional_flags': ['-a', '--progress'],
            'retry_count': 3,
            'retry_delay': 5,
            'timeout': 300,
            'date_filter': None,  # None means no filtering, or can be days like 7
        }

        for key, value in defaults.items():
            if key not in self.rsync_options:
                self.rsync_options[key] = value

    def get_applications_for_group(self, group_name: str) -> List[str]:
        """
        Get list of applications for a node group

        Args:
            group_name: Name of the node group

        Returns:
            List of application names
        """
        apps = self.node_groups.get(group_name, [])
        # Handle case where apps is None (empty YAML entry)
        return apps if apps is not None else []

    def get_log_paths_for_application(self, app_name: str) -> List[str]:
        """
        Get log paths for a specific application

        Args:
            app_name: Name of the application

        Returns:
            List of log file paths/patterns
        """
        app_config = self.applications.get(app_name, {})
        if app_config is None:
            app_config = {}
        return app_config.get('log_paths', [])

    def is_journal_enabled(self, app_name: str) -> bool:
        """
        Check if journal collection is enabled for an application

        Args:
            app_name: Name of the application

        Returns:
            True if journal collection is enabled
        """
        app_config = self.applications.get(app_name, {})
        if app_config is None:
            app_config = {}
        return app_config.get('journal', False)

    def get_journal_mode(self, app_name: str) -> str:
        """
        Get journal collection mode for an application

        Args:
            app_name: Name of the application

        Returns:
            'binary' or 'export' mode
        """
        app_config = self.applications.get(app_name, {})
        if app_config is None:
            app_config = {}
        # Check app-specific mode, fallback to default, then binary
        mode = app_config.get('journal_mode')
        if mode:
            return mode

        # Get default from journal_options
        journal_opts = self.config.get('journal_options', {})
        return journal_opts.get('default_mode', 'binary')

    def get_journal_option(self, option_name: str, default: Any = None) -> Any:
        """
        Get a journal collection option

        Args:
            option_name: Name of the option
            default: Default value if option not found

        Returns:
            Option value
        """
        journal_opts = self.config.get('journal_options', {})
        return journal_opts.get(option_name, default)

    def get_rsync_option(self, option_name: str, default: Any = None) -> Any:
        """
        Get a specific rsync option

        Args:
            option_name: Name of the option
            default: Default value if option not found

        Returns:
            Option value
        """
        return self.rsync_options.get(option_name, default)

    def get_local_storage_path(self) -> Path:
        """
        Get the local storage path for collected logs

        Returns:
            Path object for local storage
        """
        return Path(self.rsync_options.get('local_storage', 'logs'))

    def get_node_storage_path(self, hostname: str) -> Path:
        """
        Get the storage path for a specific node

        Args:
            hostname: Name of the host

        Returns:
            Path object for node-specific storage
        """
        base_path = self.get_local_storage_path()
        return base_path / hostname

    def get_app_storage_path(self, hostname: str, app_name: str) -> Path:
        """
        Get the storage path for a specific application on a node

        Args:
            hostname: Name of the host
            app_name: Name of the application

        Returns:
            Path object for application-specific storage
        """
        node_path = self.get_node_storage_path(hostname)
        return node_path / app_name

    def get_failure_log_path(self) -> Path:
        """
        Get the path for rsync failure logs (always in the data directory)

        Returns:
            Path object for failure log file
        """
        # Always place failure logs in the same directory as collected data
        log_dir = self.get_local_storage_path()
        return log_dir / "failures.log"

    def should_filter_by_date(self) -> bool:
        """
        Check if date filtering is enabled

        Returns:
            True if date filtering should be applied
        """
        return self.rsync_options.get('date_filter') is not None

    def get_date_filter_days(self) -> Optional[int]:
        """
        Get the number of days for date filtering

        Returns:
            Number of days, or None if no filtering
        """
        return self.rsync_options.get('date_filter')

    def get_ssh_connection_string(self, hostname: str) -> str:
        """
        Build SSH connection string for a host

        Args:
            hostname: Name of the host

        Returns:
            SSH connection string (user@host)
        """
        user = self.rsync_options.get('ssh_user', 'root')
        return f"{user}@{hostname}"

    def get_gateway_host(self) -> Optional[str]:
        """
        Get gateway host configuration

        Returns:
            Gateway hostname or None if not configured
        """
        return self.rsync_options.get('gateway_host')

    def get_gateway_user(self) -> str:
        """
        Get gateway user (defaults to ssh_user if not specified)

        Returns:
            Gateway username
        """
        gateway_user = self.rsync_options.get('gateway_user')
        if gateway_user:
            return gateway_user
        return self.rsync_options.get('ssh_user', 'root')

    def get_gateway_port(self) -> int:
        """
        Get gateway SSH port

        Returns:
            Gateway SSH port number
        """
        return self.rsync_options.get('gateway_port', 22)

    def is_gateway_enabled(self) -> bool:
        """
        Check if gateway/proxy is configured

        Returns:
            True if gateway is configured
        """
        return self.get_gateway_host() is not None

    def get_rsync_base_flags(self) -> List[str]:
        """
        Get base rsync flags from configuration

        Returns:
            List of rsync command flags
        """
        flags = self.rsync_options.get('additional_flags', ['-a']).copy()

        # Add compression flag if enabled in config
        use_compression = self.rsync_options.get('use_compression', False)
        if use_compression and '-z' not in flags and '--compress' not in flags:
            flags.append('-z')

        return flags

    def validate(self) -> List[str]:
        """
        Validate the configuration

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if not self.applications:
            errors.append("No applications defined in configuration")

        if not self.node_groups:
            errors.append("No node groups defined in configuration")

        # Check that all applications referenced in node_groups exist
        for group, apps in self.node_groups.items():
            # Handle case where apps is None (empty YAML entry)
            if apps is None:
                continue  # Skip groups with no applications defined
            for app in apps:
                if app not in self.applications:
                    errors.append(
                        f"Application '{app}' in group '{group}' not defined in applications section")

        # Check that all applications have log paths or journal enabled
        for app_name, app_config in self.applications.items():
            # Handle case where app_config is None (empty YAML entry)
            if app_config is None:
                app_config = {}
            
            has_log_paths = 'log_paths' in app_config and app_config['log_paths']
            has_journal = app_config.get('journal', False)

            if not has_log_paths and not has_journal:
                # Only warn about empty applications if they're actually used in node_groups
                app_used = False
                for group, apps in self.node_groups.items():
                    # Handle case where apps is None (empty YAML entry)
                    if apps is not None and app_name in apps:
                        app_used = True
                        break
                
                if app_used:
                    errors.append(
                        f"Application '{app_name}' is used in node groups but has no log_paths and journal is not enabled")
                # If not used, just ignore it (no error)

        return errors


if __name__ == "__main__":
    # Example usage
    config = ConfigManager("config/config.yaml")
    try:
        config.load()
        errors = config.validate()
        if errors:
            print("Configuration errors:")
            for error in errors:
                print(f"  - {error}")
        else:
            print("Configuration loaded successfully")
            print(f"Applications: {list(config.applications.keys())}")
            print(f"Node groups: {list(config.node_groups.keys())}")
    except FileNotFoundError as e:
        print(f"Error: {e}")
