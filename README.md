# log-puller

A utility for collecting logs from heterogeneous nodes using rsync, with support for Ansible inventory parsing, parallel execution, and systemd journal collection (binary and export modes).

## Features

- **Ansible Integration**: Parse Ansible inventory files to determine which applications run on which nodes
- **Parallel Execution**: Configurable concurrent rsync jobs for efficient log collection
- **Systemd Journal Collection**: 
  - **Binary mode** (RECOMMENDED): Copy journal files directly - minimal remote impact, complete data
  - **Export mode**: Use journalctl to export logs with unit/time filtering
- **Retry Logic**: Automatic retry on failures with comprehensive error logging
- **Incremental Compression**: Local-side compression that only archives new files
- **Multiple Modes**: Normal sync, dry-run, and explore modes
- **Rotated Logs**: Automatically handles log rotation (`.1`, `.gz`, etc.)
- **Per-Node Organization**: Collected logs organized by hostname and application

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
  - [Inventory File](#inventory-file)
  - [Configuration File](#configuration-file)
  - [Systemd Journal Collection](#systemd-journal-collection)
- [Usage](#usage)
- [Configuration Examples](#configuration-examples)
- [Directory Structure](#directory-structure)
- [Troubleshooting](#troubleshooting)
- [Advanced Features](#advanced-features)

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

#### 1. Applications and their log sources

```yaml
applications:
  # Traditional file-based logs only
  postgresql:
    log_paths:
      - /var/log/postgresql/*.log
    journal: false
  
  # Both file-based and journal (binary mode - RECOMMENDED)
  system:
    log_paths:
      - /var/log/syslog*
    journal: true
    journal_mode: binary  # Copy journal files directly
  
  # Journal export mode (for specific services)
  nginx:
    log_paths:
      - /var/log/nginx/*.log*
    journal: true
    journal_mode: export  # Use journalctl export
```

#### 2. Node groups and their applications

```yaml
node_groups:
  # Special group: applies to ALL nodes automatically
  _all_nodes:
    - system
  
  bookkeepingdb:
    - postgresql
  
  webservers:
    - nginx
```

**Note**: The special `_all_nodes` group automatically applies to every node, regardless of their inventory group. Perfect for system logs or monitoring agents.

#### 3. Rsync and collection options

```yaml
rsync_options:
  max_parallel_jobs: 5
  compress: false  # Set to true to auto-compress after each sync
  use_compression: false  # Set to true to enable rsync -z compression during transfer
  local_storage: /var/logs/collected  # Absolute or relative path
  ssh_user: root
  ssh_port: 22
  ssh_ignore_host_key: true  # WARNING: reduces security
  retry_count: 3
  retry_delay: 5
  timeout: 300
  date_filter: null  # Or number of days (e.g., 7)
```

#### 4. Journal collection options

```yaml
journal_options:
  # Default mode: 'binary' (RECOMMENDED) or 'export'
  default_mode: binary
  
  binary:
    # Single path or list of paths
    # Supports both persistent (/var/log/journal/) and volatile (/run/log/journal/)
    remote_journal_path:
      - /var/log/journal/  # Persistent (survives reboot)
      - /run/log/journal/  # Volatile (runtime only)
    # Or single path: remote_journal_path: /var/log/journal/
    current_boot_only: true
  
  export:
    output_format: export  # or 'json'
    ssh_compression: true
    priority_filter: null
    max_lines: null
```

### Systemd Journal Collection

Modern Linux distributions use systemd journal. log-puller supports two collection modes:

#### Binary Mode (RECOMMENDED - Default)

**Copies journal files directly via rsync**:
- ✅ **Minimal remote impact**: Just file copy, no journalctl processing
- ✅ **Complete data**: All metadata, structured fields, binary attachments
- ✅ **Efficient**: Binary format is compact and compresses well
- ✅ **Incremental sync**: rsync only transfers changed files
- ✅ **Local processing**: Parse with Logstash/journalctl locally with full flexibility
- ✅ **Multiple paths**: Supports both persistent and volatile journal locations

```yaml
applications:
  system:
    journal: true
    journal_mode: binary  # or omit (binary is default)

journal_options:
  binary:
    # Collect from both persistent and volatile journals
    remote_journal_path:
      - /var/log/journal/  # Persistent (survives reboot)
      - /run/log/journal/  # Volatile (runtime, lost on reboot)
```

**After collection, process locally**:
```bash
# Export to JSON for Logstash
journalctl --directory=/data/logs/host1/system/journal \
           --output=json \
           --no-pager > host1_journal.json

# Filter by service
journalctl --directory=/data/logs/host1/system/journal \
           --unit=nginx.service \
           --output=json > host1_nginx.json
```

#### Export Mode

**Uses journalctl on remote to export logs**:
- Processes logs on remote node (more CPU usage)
- Can filter by unit/time during collection
- Outputs text format (export or JSON)
- Useful for specific service logs

```yaml
applications:
  nginx:
    journal: true
    journal_mode: export
```

**Automatic unit mapping**:
| Application | Systemd Unit |
|------------|--------------|
| nginx | nginx.service |
| apache | apache2.service |
| postgresql | postgresql.service |
| mysql | mysql.service |
| docker | docker.service |
| ssh | sshd.service |
| system | (all units) |

#### When to use which mode

**Use binary mode for**:
- System-wide journal collection
- Logstash/Elasticsearch pipelines
- Maximum processing flexibility
- Production systems (minimal impact)

**Use export mode for**:
- Quick text exports
- Specific service logs with filtering
- Testing/development

**Performance comparison**:

| Aspect | Binary Mode | Export Mode |
|--------|------------|-------------|
| Remote CPU | **Minimal** | Medium |
| Remote I/O | **Low** | Medium |
| Network Transfer | **Small** | Larger |
| Incremental Sync | **Yes** | No |
| Processing Flexibility | **Maximum** | Limited |

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
- `--compress` - Compress collected logs after syncing (creates tar.gz archives)
- `--show-summary` - Display configuration summary and exit
- `-v, --verbose` - Enable verbose output

**Examples**:

```bash
# Normal sync (no compression)
./main.py sync

# Sync with automatic compression
./main.py sync --compress

# Dry-run to preview
./main.py sync --dry-run

# Show configuration
./main.py sync --show-summary

# Verbose output
./main.py sync -v
```

### Explore Mode

Check if remote log files exist without copying:

```bash
./main.py explore
```

### Compress Mode

Compress already-collected logs (incremental):

```bash
./main.py compress
```

**Options**:
- `--force` - Re-compress all files, ignoring tracking

### List Archives

View available compressed archives:

```bash
./main.py list-archives

# Filter by host
./main.py list-archives --host hostname
```

## Configuration Examples

### Example 1: System Logs from All Nodes

```yaml
applications:
  system:
    log_paths:
      - /var/log/syslog*
      - /var/log/messages*
    journal: true
    journal_mode: binary

node_groups:
  _all_nodes:
    - system  # Collected from EVERY node
```

### Example 2: Mix of Universal and Group-Specific

```yaml
applications:
  system:
    log_paths:
      - /var/log/syslog*
    journal: true
    journal_mode: binary
  
  nginx:
    log_paths:
      - /var/log/nginx/*.log*
    journal: true
    journal_mode: export
  
  postgresql:
    log_paths:
      - /var/log/postgresql/*.log

node_groups:
  _all_nodes:
    - system
  
  webservers:
    - nginx
  
  databases:
    - postgresql
```

Result:
- **webserver01** gets: system (files + journal) + nginx (files + journal export)
- **db01** gets: system (files + journal) + postgresql (files only)

### Example 3: Journal-only Collection

```yaml
applications:
  ssh:
    journal: true
    journal_mode: export

node_groups:
  _all_nodes:
    - ssh
```

### Example 4: With Time Filtering

```yaml
applications:
  system:
    journal: true

rsync_options:
  date_filter: 3  # Only last 3 days
```

This applies to both rsync and journal export mode (passes `--since='3 days ago'` to journalctl).

### Example 5: Logstash Pipeline Configuration

After collecting binary journals, process them for Logstash:

```bash
#!/bin/bash
# process_journals.sh

for host_dir in /data/logs/*/; do
  host=$(basename "$host_dir")
  journal_dir="$host_dir/system/journal"
  
  if [ -d "$journal_dir" ]; then
    journalctl --directory="$journal_dir" \
               --output=json \
               --since="7 days ago" \
               --no-pager > "/data/logstash/input/${host}_journal.json"
  fi
done
```

**Logstash configuration**:

```ruby
input {
  file {
    path => "/data/logstash/input/*_journal.json"
    codec => "json"
    start_position => "beginning"
  }
}

filter {
  mutate {
    rename => {
      "MESSAGE" => "message"
      "__REALTIME_TIMESTAMP" => "timestamp_us"
      "PRIORITY" => "priority"
      "SYSLOG_IDENTIFIER" => "program"
    }
  }
  
  date {
    match => [ "timestamp_us", "UNIX_MS" ]
    target => "@timestamp"
  }
}

output {
  elasticsearch {
    hosts => ["localhost:9200"]
    index => "syslogs-%{+YYYY.MM.dd}"
  }
}
```

## Directory Structure

### Project Structure

```
log-puller/
├── config/
│   ├── config.yaml          # Main configuration
│   └── hosts               # Ansible inventory
├── src/                    # Source code
│   ├── config_manager.py
│   ├── inventory_parser.py
│   ├── journal_collector.py
│   ├── log_collector.py
│   ├── rsync_manager.py
│   └── compression_manager.py
├── main.py                 # CLI entry point
└── README.md
```

### Collected Logs Structure

```
/data/logs/                    # Or your configured path
├── host1/
│   ├── system/
│   │   ├── syslog            # File-based logs
│   │   ├── journal_0/        # Persistent journal (binary mode, path 1)
│   │   │   └── <machine-id>/
│   │   │       └── system.journal
│   │   └── journal_1/        # Volatile journal (binary mode, path 2)
│   │       └── <machine-id>/
│   │           └── system.journal
│   └── nginx/
│       ├── access.log        # File-based logs
│       └── journal_nginx.service_20251004_100530.log  # Export mode
├── host2/
│   └── ...
├── archives/
│   ├── host1_20251001_143000.tar.gz
│   └── host2_20251001_143015.tar.gz
└── failures.log              # If any syncs failed
```

**Note**: When multiple `remote_journal_path` entries are configured, they are stored in separate subdirectories (`journal_0`, `journal_1`, etc.). When using a single path, journals are stored in `journal/`.

## Troubleshooting

### Connection Issues

```bash
# Test SSH connectivity
ssh root@hostname

# Check if log paths exist
./main.py explore
```

### Permission Denied

Ensure the SSH user has read permissions:

```bash
# On remote host
chmod 644 /var/log/app/*.log

# For journal access
sudo usermod -a -G systemd-journal your-ssh-user
```

### No Journal Output

Test manually on remote:

```bash
ssh user@host journalctl --no-pager --lines=10
```

Possible causes:
1. journalctl not available (non-systemd system)
2. Permission denied for journal access
3. Time filter excludes all entries

### Large Journal Files

If binary journal files are too large:

1. Enable `current_boot_only: true` in journal_options
2. Reduce `date_filter` to fewer days
3. Use export mode with specific unit filters instead

### No Files Synced

1. Check inventory file syntax
2. Verify node groups match in both `hosts` and `config.yaml`
3. Use explore mode to verify remote paths
4. Check `failures.log` for errors

## Advanced Features

### Universal Applications (_all_nodes)

The special `_all_nodes` group applies to every node automatically:

```yaml
node_groups:
  _all_nodes:
    - system        # System logs from all nodes
    - monitoring    # Monitoring agent logs

The `_all_nodes` applications are collected **in addition to** group-specific applications.

### Incremental Compression

The compression system tracks which files have been archived:
- Only new/modified files are added to archives
- Tracking files: `archives/.{hostname}_tracked.txt`
- Each archive is timestamped: `{hostname}_{timestamp}.tar.gz`

### Error Handling

- Failed syncs are logged to `failures.log`
- Automatic retry with configurable attempts and delays
- Continues on individual failures (doesn't abort entire run)

### SSH Configuration

The tool uses SSH for connections. Ensure:
- SSH keys are configured for passwordless access
- User has permission to read log files

**Host Key Verification**:

By default, ignores SSH host key verification (`ssh_ignore_host_key: true`). 

To enable strict checking (more secure):
```yaml
rsync_options:
  ssh_ignore_host_key: false
```

Then ensure all hosts are in `~/.ssh/known_hosts`:
```bash
ssh-keyscan hostname >> ~/.ssh/known_hosts
```

### Date Filtering

Apply to both rsync and journal collection:

```yaml
rsync_options:
  date_filter: 7  # Only last 7 days
```

### Scheduled Execution

Create a script for periodic execution:

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
- For journal collection: systemd-based systems with `journalctl`

## License

This project is provided as-is for log collection purposes.

## Contributing

Feel free to submit issues or pull requests for improvements!
