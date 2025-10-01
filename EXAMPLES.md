# Example Configuration Scenarios

## Scenario 1: System Logs from All Nodes

If you want to collect system logs from every node, regardless of what applications they run:

```yaml
applications:
  system:
    log_paths:
      - /var/log/syslog*
      - /var/log/messages*
      - /var/log/auth.log*

node_groups:
  _all_nodes:
    - system  # Collected from EVERY node
```

## Scenario 2: Mix of Universal and Group-Specific

```yaml
applications:
  system:
    log_paths:
      - /var/log/syslog*
  
  monitoring:
    log_paths:
      - /var/log/node_exporter/*.log
  
  nginx:
    log_paths:
      - /var/log/nginx/*.log*
  
  postgresql:
    log_paths:
      - /var/log/postgresql/*.log

node_groups:
  # These are collected from ALL nodes
  _all_nodes:
    - system
    - monitoring
  
  # These are only collected from nodes in specific groups
  webservers:
    - nginx
  
  databases:
    - postgresql
```

Result:
- **webserver01** (in [webservers] group) gets: system, monitoring, nginx
- **db01** (in [databases] group) gets: system, monitoring, postgresql
- **app01** (in any other group) gets: system, monitoring

## Scenario 3: Your Bookkeeping Setup

Based on your actual inventory:

```yaml
applications:
  system:
    log_paths:
      - /var/log/syslog*
      - /var/log/messages*
  
  postgresql:
    log_paths:
      - /var/log/postgresql/*.log
  
  bookkeeping:
    log_paths:
      - /opt/bookkeeping/logs/*.log
  
  nginx:
    log_paths:
      - /var/log/nginx/*.log*

node_groups:
  # Every node gets system logs
  _all_nodes:
    - system
  
  # alio2-cr1-hv-bdb01 also gets these:
  bookkeepingdb:
    - postgresql
    - bookkeeping
  
  # alio2-cr1-hv-web02 also gets these:
  bookkeeping:
    - nginx
    - bookkeeping
```

This means:
- **alio2-cr1-hv-bdb01**: system + postgresql + bookkeeping logs
- **alio2-cr1-hv-web02**: system + nginx + bookkeeping logs

## Scenario 4: No Universal Applications

If you don't want any universal applications, simply don't define `_all_nodes`:

```yaml
node_groups:
  webservers:
    - nginx
  
  databases:
    - postgresql
```

The `_all_nodes` group is completely optional!
