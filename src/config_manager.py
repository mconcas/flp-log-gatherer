"""
Configuration manager for log-puller
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
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        
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
            'compress': True,
            'local_storage': 'logs',
            'ssh_user': 'root',
            'ssh_port': 22,
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
        return self.node_groups.get(group_name, [])
    
    def get_log_paths_for_application(self, app_name: str) -> List[str]:
        """
        Get log paths for a specific application
        
        Args:
            app_name: Name of the application
            
        Returns:
            List of log file paths/patterns
        """
        app_config = self.applications.get(app_name, {})
        return app_config.get('log_paths', [])
    
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
    
    def get_rsync_base_flags(self) -> List[str]:
        """
        Get base rsync flags from configuration
        
        Returns:
            List of rsync command flags
        """
        flags = self.rsync_options.get('additional_flags', ['-a'])
        
        # Add compression flag if enabled (but note: we do local compression)
        # For rsync, we might not want network compression to avoid remote CPU load
        
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
            for app in apps:
                if app not in self.applications:
                    errors.append(f"Application '{app}' in group '{group}' not defined in applications section")
        
        # Check that all applications have log paths
        for app_name, app_config in self.applications.items():
            if 'log_paths' not in app_config or not app_config['log_paths']:
                errors.append(f"Application '{app_name}' has no log_paths defined")
        
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
