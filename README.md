# log-puller

A utility for collecting logs from heterogeneous nodes using rsync, with support for Ansible inventory parsing and parallel execution.

## Features

- **Ansible Integration**: Parse Ansible inventory files to determine which applications run on which nodes
- **Parallel Execution**: Configurable concurrent rsync jobs for efficient log collection
- **Retry Logic**: Automatic retry on failures with comprehensive error logging
- **Incremental Compression**: Local-side compression that only archives new files
- **Multiple Modes**: Normal sync, dry-run, and explore modes
- **Rotated Logs**: Automatically handles log rotation (`.1`, `.gz`, etc.)
- **Per-Node Organization**: Collected logs organized by hostname and application

## Installation

1. Clone or download this repository
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Configure your inventory and settings (see Configuration section)

## Quick Start

1. **Copy example configuration files**:

```bash
cp config/hosts.example config/hosts
# Edit config/hosts with your actual nodes
```

2. **Review and customize** `config/config.yaml` for your environment

3. **Explore remote files** (check if log paths exist):

```bash
./main.py explore
```

4. **Perform a dry-run** (see what would be synced):

```bash
./main.py sync --dry-run
```

5. **Collect logs**:

```bash
./main.py sync
```

## Configuration

### Inventory File (`config/hosts`)

The inventory file uses Ansible INI format with group headers:

```ini
[bookkeepingdb]
alio2-cr1-hv-bdb01

[bookkeeping]
alio2-cr1-hv-web02

[webservers]
web01.example.com
web02.example.com
```

### Configuration File (`config/config.yaml`)

The configuration file defines:

1. **Applications and their log paths**:

```yaml
applications:
  postgresql:
    log_paths:
      - /var/log/postgresql/*.log
  nginx:
    log_paths:
      - /var/log/nginx/access.log*
      - /var/log/nginx/error.log*
```

2. **Node groups and their applications**:

```yaml
node_groups:
  bookkeepingdb:
    - postgresql
    - system
  webservers:
    - nginx
    - apache
```

3. **Rsync options**:

```yaml
rsync_options:
  max_parallel_jobs: 5
  compress: true
  local_storage: logs
  ssh_user: root
  ssh_port: 22
  retry_count: 3
  retry_delay: 5
  timeout: 300
```

## Usage

### Command Overview

```bash
./main.py <command> [options]
```

Available commands:
- `sync` - Synchronize logs from remote hosts (default)
- `explore` - Check if remote log files exist
- `compress` - Compress already-collected logs
- `list-archives` - List available archives

### Sync Mode (Default)

Collect logs from all configured nodes:

```bash
./main.py sync
```

**Options**:
- `--dry-run` - Show what would be synced without copying files
- `--no-compress` - Skip compression of collected logs
- `--show-summary` - Display configuration summary and exit
- `-v, --verbose` - Enable verbose output

**Examples**:

```bash
# Normal sync with compression
./main.py sync

# Dry-run to preview
./main.py sync --dry-run

# Sync without compression
./main.py sync --no-compress

# Show what would be synced
./main.py sync --show-summary

# Verbose output
./main.py sync -v
```

### Explore Mode

Check if remote log files exist without copying:

```bash
./main.py explore
```

This mode:
- Connects to each node via SSH
- Lists files matching configured log paths
- Reports which applications/logs are available
- Useful for validating configuration before syncing

**Example output**:
```
Host: alio2-cr1-hv-bdb01
  [postgresql] ✓ EXISTS
    Remote path: /var/log/postgresql/*.log
    Files found:
      -rw-r--r-- 1 postgres postgres 1.2M Oct 1 10:30 postgresql-main.log
      -rw-r--r-- 1 postgres postgres 256K Oct 1 09:15 postgresql-main.log.1
```

### Compress Mode

Compress already-collected logs (incremental):

```bash
./main.py compress
```

**Options**:
- `--force` - Re-compress all files, ignoring tracking

By default, only new files (not previously archived) are compressed. Use `--force` to create a full archive.

**Examples**:

```bash
# Incremental compression (only new files)
./main.py compress

# Force full re-compression
./main.py compress --force
```

### List Archives

View available compressed archives:

```bash
./main.py list-archives
```

**Options**:
- `--host <hostname>` - Filter archives for specific host

**Examples**:

```bash
# List all archives
./main.py list-archives

# List archives for specific host
./main.py list-archives --host alio2-cr1-hv-bdb01
```

## Directory Structure

After running log collection, your directory will look like:

```
log-puller/
├── logs/
│   ├── alio2-cr1-hv-bdb01/
│   │   ├── postgresql/
│   │   │   └── postgresql-main.log
│   │   └── system/
│   │       └── syslog
│   ├── alio2-cr1-hv-web02/
│   │   ├── nginx/
│   │   │   ├── access.log
│   │   │   └── error.log
│   │   └── bookkeeping/
│   │       └── app.log
│   └── archives/
│       ├── alio2-cr1-hv-bdb01_20251001_143000.tar.gz
│       └── alio2-cr1-hv-web02_20251001_143015.tar.gz
└── logs/failures.log  # If any syncs failed
```

## How It Works

1. **Parse Inventory**: Reads Ansible hosts file to identify nodes and their groups
2. **Load Configuration**: Maps node groups to applications and log paths
3. **Build Jobs**: Creates rsync jobs for each host/application/log combination
4. **Execute Parallel**: Runs rsync operations with configurable parallelism
5. **Retry Failed**: Automatically retries failed syncs with exponential backoff
6. **Compress**: Creates incremental tar.gz archives of collected logs
7. **Track**: Maintains state of archived files to avoid duplication

## Advanced Features

### Incremental Compression

The compression system tracks which files have been archived:
- Only new/modified files are added to archives
- Tracking files: `logs/archives/.{hostname}_tracked.txt`
- Each archive is timestamped: `{hostname}_{timestamp}.tar.gz`

### Error Handling

- Failed syncs are logged to `logs/failures.log`
- Automatic retry with configurable attempts and delays
- Continues on individual failures (doesn't abort entire run)

### SSH Configuration

The tool uses SSH for connections. Ensure:
- SSH keys are configured for passwordless access
- User has permission to read log files (default: root)
- Hosts are in `known_hosts` or SSH is configured to accept them

### Customization

You can customize rsync behavior via `config.yaml`:
- Add flags: `--exclude`, `--include`, `--bwlimit`, etc.
- Adjust timeout for slow connections
- Configure retry behavior
- Set date filters to only sync recent logs

## Troubleshooting

### Connection Issues

```bash
# Test SSH connectivity
ssh root@hostname

# Check if log paths exist
./main.py explore
```

### Permission Denied

Ensure the SSH user has read permissions on log directories:

```bash
# On remote host
chmod 644 /var/log/app/*.log
```

### No Files Synced

1. Check inventory file syntax
2. Verify node groups match in both `hosts` and `config.yaml`
3. Use explore mode to verify remote paths
4. Check `logs/failures.log` for errors

### Archives Not Created

- Ensure `compress: true` in `config.yaml`
- Check that logs were successfully synced first
- Look for errors in verbose output: `./main.py sync -v`

## Examples

### Basic Workflow

```bash
# 1. Verify configuration
./main.py sync --show-summary

# 2. Check remote files
./main.py explore

# 3. Dry-run
./main.py sync --dry-run

# 4. Actual sync
./main.py sync

# 5. List created archives
./main.py list-archives
```

### Scheduled Execution

Create a bash script for periodic execution:

```bash
#!/bin/bash
# /usr/local/bin/collect-logs.sh

cd /path/to/log-puller
./main.py sync >> /var/log/log-puller.log 2>&1
```

Add to crontab:

```bash
# Collect logs daily at 2 AM
0 2 * * * /usr/local/bin/collect-logs.sh
```

## Requirements

- Python 3.7+
- `rsync` installed on both local and remote systems
- SSH access to remote nodes
- PyYAML library

## License

This project is provided as-is for log collection purposes.

## Contributing

Feel free to submit issues or pull requests for improvements!
