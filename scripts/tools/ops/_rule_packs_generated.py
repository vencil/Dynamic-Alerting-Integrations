"""GENERATED from rule-packs/threshold-registry.yaml — DO NOT EDIT.

The threshold half of scaffold_tenant.RULE_PACKS (D-A PR-3): per pack its
display/exporter/default_on/rule_pack_file and the `defaults` /
`optional_overrides` tiers. Edit the registry, then run
  python3 scripts/tools/lint/check_threshold_registry.py --regen
`--ci` (pre-commit, CI) fails while this file is stale.
"""

THRESHOLD_PACKS = {'kubernetes': {'display': 'Kubernetes (cAdvisor + KSM)',
                'exporter': 'cAdvisor + kube-state-metrics',
                'default_on': True,
                'rule_pack_file': 'rule-packs/rule-pack-kubernetes.yaml',
                'defaults': {'container_cpu': {'value': 80,
                                               'unit': '%',
                                               'desc': 'Container CPU % of limit (weakest link)',
                                               'metric_class': 'saturation',
                                               'dimensions': ['version']},
                             'container_cpu_throttle': {'value': 25,
                                                        'unit': '%',
                                                        'desc': 'Chronic CFS throttle: % of ACTIVE '
                                                                '100ms periods throttled, NOT % '
                                                                'CPU lost (critical tier opt-in '
                                                                'via '
                                                                'container_cpu_throttle_critical, '
                                                                'declared in optional_overrides; '
                                                                '#944)',
                                                        'metric_class': 'saturation'},
                             'container_memory': {'value': 85,
                                                  'unit': '%',
                                                  'desc': 'Container memory % of limit (weakest '
                                                          'link)',
                                                  'metric_class': 'saturation',
                                                  'dimensions': ['version']}},
                'optional_overrides': {'container_cpu_critical': {'value': 95,
                                                                  'unit': '%',
                                                                  'desc': 'Container CPU critical',
                                                                  'metric_class': 'saturation'},
                                       'container_cpu_throttle_critical': {'value': 50,
                                                                           'unit': '%',
                                                                           'desc': 'CFS throttle '
                                                                                   'critical',
                                                                           'metric_class': 'saturation'},
                                       'container_memory_critical': {'value': 95,
                                                                     'unit': '%',
                                                                     'desc': 'Container memory '
                                                                             'critical',
                                                                     'metric_class': 'saturation'}}},
 'postgresql': {'display': 'PostgreSQL (postgres_exporter)',
                'exporter': 'prometheus-community/postgres_exporter',
                'default_on': False,
                'rule_pack_file': 'rule-packs/rule-pack-postgresql.yaml',
                'defaults': {'pg_connections': {'value': 80,
                                                'unit': '% of max_connections',
                                                'desc': 'Connection usage % warning',
                                                'metric_class': 'saturation'},
                             'pg_replication_lag': {'value': 30,
                                                    'unit': 'seconds',
                                                    'desc': 'Replication lag warning',
                                                    'metric_class': 'replication'}},
                'optional_overrides': {'pg_connections_critical': {'value': 90,
                                                                   'unit': '% of max_connections',
                                                                   'desc': 'Critical tier '
                                                                           'connections',
                                                                   'metric_class': 'saturation'},
                                       'pg_replication_lag_critical': {'value': 60,
                                                                       'unit': 'seconds',
                                                                       'desc': 'Critical tier '
                                                                               'replication lag',
                                                                       'metric_class': 'replication'}}},
 'mariadb': {'display': 'MariaDB / MySQL (mysqld_exporter)',
             'exporter': 'prom/mysqld-exporter or Percona',
             'default_on': True,
             'rule_pack_file': 'rule-packs/rule-pack-mariadb.yaml',
             'defaults': {'mysql_connection_usage_percent': {'value': 80,
                                                             'unit': '% of max_connections',
                                                             'desc': 'Connection usage warning: '
                                                                     'threads_connected as a share '
                                                                     'of max_connections, per '
                                                                     'instance, busiest instance '
                                                                     'per tenant; 80 = PMM "MySQL '
                                                                     'connections in use" template '
                                                                     '/ awesome-prometheus-alerts. '
                                                                     'Replaces the absolute-count '
                                                                     'key mysql_connections '
                                                                     '(#3778: a unit change is a '
                                                                     'removal plus an addition, no '
                                                                     'alias)',
                                                             'metric_class': 'saturation'},
                          'mysql_replication_lag': {'value': 30,
                                                    'unit': 'seconds',
                                                    'desc': 'Async replication lag warning '
                                                            '(seconds behind primary, '
                                                            'sql_delay-adjusted); 30 = '
                                                            'APA/mysqld-mixin consensus trigger '
                                                            '(they page critical at 30s; demoted '
                                                            "to warning — PMM's lag-template "
                                                            'family default is 600s warning). '
                                                            'Enabled by default (#1200 WS5-P0-a '
                                                            'Q3=C)',
                                                    'metric_class': 'replication'}},
             'optional_overrides': {'mysql_threads_running': {'value': 30,
                                                              'unit': 'threads',
                                                              'desc': 'threads_running saturation, '
                                                                      'NOT host CPU% — '
                                                                      'running-threads 1m-avg '
                                                                      'warning; 30 = PMM/Nichter '
                                                                      "'high' tier, a reference "
                                                                      'start to calibrate per host '
                                                                      '(a healthy count scales '
                                                                      'with CPU cores); renamed '
                                                                      'from mysql_cpu (2-release '
                                                                      'alias window, old spelling '
                                                                      'still resolves)',
                                                              'metric_class': 'saturation'},
                                    'mysql_connection_usage_percent_critical': {'value': 90,
                                                                                'unit': '% of '
                                                                                        'max_connections',
                                                                                'desc': 'Critical '
                                                                                        'tier '
                                                                                        'connection '
                                                                                        'usage '
                                                                                        '(same '
                                                                                        'tier pair '
                                                                                        'as '
                                                                                        'pg_connections_critical)',
                                                                                'metric_class': 'saturation'},
                                    'mysql_threads_running_critical': {'value': 50,
                                                                       'unit': 'threads',
                                                                       'desc': 'Critical tier '
                                                                               '(running-threads '
                                                                               'saturation); '
                                                                               '120→50 = PMM '
                                                                               'critical-load / '
                                                                               'Nichter '
                                                                               "'overloaded' "
                                                                               '(#944); renamed '
                                                                               'from '
                                                                               'mysql_cpu_critical '
                                                                               '(#1231, 2-release '
                                                                               'alias window)',
                                                                       'metric_class': 'saturation'},
                                    'mysql_replication_lag_critical': {'value': 120,
                                                                       'unit': 'seconds',
                                                                       'desc': 'Critical tier '
                                                                               'replication lag; '
                                                                               '120 = 4x the '
                                                                               'APA/mixin 30s '
                                                                               'trigger, 5x below '
                                                                               'PMM family 600s — '
                                                                               'sustained '
                                                                               'minutes-level lag '
                                                                               'is a failover '
                                                                               'data-loss window '
                                                                               '(#1200 WS5-P0-a)',
                                                                       'metric_class': 'replication'}}},
 'redis': {'display': 'Redis (oliver006/redis_exporter)',
           'exporter': 'oliver006/redis_exporter',
           'default_on': False,
           'rule_pack_file': 'rule-packs/rule-pack-redis.yaml',
           'defaults': {'redis_memory_used_bytes': {'value': 4294967296,
                                                    'unit': 'bytes (4GB)',
                                                    'desc': 'Memory usage warning',
                                                    'metric_class': 'saturation'},
                        'redis_connected_clients': {'value': 200,
                                                    'unit': 'count',
                                                    'desc': 'Connected clients warning',
                                                    'metric_class': 'saturation'},
                        'redis_evicted_keys_rate': {'value': 100,
                                                    'unit': 'keys/s',
                                                    'desc': 'Key eviction rate; #1196 B identity '
                                                            'fix: renamed from '
                                                            'redis_evicted_keys_total (alert '
                                                            'demands _rate) + moved '
                                                            'optional_overrides→defaults — unwired '
                                                            'dead key, no alias',
                                                    'metric_class': 'efficiency'},
                        'redis_replication_lag': {'value': 30,
                                                  'unit': 'seconds',
                                                  'desc': 'Replica lag warning; 30 = same anchor '
                                                          'as mysql/pg replication_lag — redis '
                                                          'replication is normally sub-second, so '
                                                          '30s is unambiguous trouble '
                                                          '(noise-averse pick, #1231 owner call); '
                                                          '#1196 C identity fix, no alias',
                                                  'metric_class': 'replication'}},
           'optional_overrides': {'redis_connected_clients_critical': {'value': 300,
                                                                       'unit': 'count',
                                                                       'desc': 'Connected clients '
                                                                               'critical',
                                                                       'metric_class': 'saturation'},
                                  'redis_memory_used_bytes_critical': {'value': 5368709120,
                                                                       'unit': 'bytes',
                                                                       'desc': 'Memory usage '
                                                                               'critical',
                                                                       'metric_class': 'saturation'},
                                  'redis_replication_lag_critical': {'value': 50,
                                                                     'unit': 'seconds',
                                                                     'desc': 'Replica lag critical',
                                                                     'metric_class': 'replication'}}},
 'mongodb': {'display': 'MongoDB (percona/mongodb_exporter)',
             'exporter': 'percona/mongodb_exporter',
             'default_on': False,
             'rule_pack_file': 'rule-packs/rule-pack-mongodb.yaml',
             'defaults': {'mongodb_connections_current': {'value': 300,
                                                          'unit': 'count',
                                                          'desc': 'Current connections warning',
                                                          'metric_class': 'saturation'},
                          'mongodb_replication_lag': {'value': 10,
                                                      'unit': 'seconds',
                                                      'desc': 'Replication lag warning; #1196 C '
                                                              'identity fix: renamed from '
                                                              'mongodb_repl_lag_seconds (alert '
                                                              'demands mongodb_replication_lag) — '
                                                              'unwired dead key, no alias',
                                                      'metric_class': 'replication'},
                          'mongodb_opcounters_rate': {'value': 10000,
                                                      'unit': 'ops/s',
                                                      'desc': 'Total operations rate; #1196 B '
                                                              'identity fix: renamed from '
                                                              'mongodb_opcounters_total (alert '
                                                              'demands _rate) + moved '
                                                              'optional_overrides→defaults — '
                                                              'unwired dead key, no alias',
                                                      'metric_class': 'throughput'}},
             'optional_overrides': {'mongodb_connections_current_critical': {'value': 450,
                                                                             'unit': 'count',
                                                                             'desc': 'Current '
                                                                                     'connections '
                                                                                     'critical',
                                                                             'metric_class': 'saturation'},
                                    'mongodb_replication_lag_critical': {'value': 30,
                                                                         'unit': 'seconds',
                                                                         'desc': 'Replication lag '
                                                                                 'critical',
                                                                         'metric_class': 'replication'}}},
 'elasticsearch': {'display': 'Elasticsearch (elasticsearch_exporter)',
                   'exporter': 'justwatchcom/elasticsearch_exporter',
                   'default_on': False,
                   'rule_pack_file': 'rule-packs/rule-pack-elasticsearch.yaml',
                   'defaults': {'es_heap_usage_percent': {'value': 85,
                                                          'unit': '%',
                                                          'desc': 'JVM heap usage warning; #1196 C '
                                                                  'identity fix: renamed from '
                                                                  'es_jvm_memory_used_percent '
                                                                  '(alert demands '
                                                                  'es_heap_usage_percent) — '
                                                                  'unwired dead key, no alias',
                                                          'metric_class': 'saturation'},
                                'es_disk_usage_percent': {'value': 85,
                                                          'unit': '%',
                                                          'desc': 'Disk USAGE % warning (used = 1 '
                                                                  '- available/size, recording '
                                                                  'rule '
                                                                  'tenant:es_disk_usage_percent:max, '
                                                                  'compared with >); 85 = same '
                                                                  'band as oracle/db2 '
                                                                  'tablespace_used_percent; #1196 '
                                                                  'C semantic flip: replaces '
                                                                  'es_filesystem_free_percent 15 '
                                                                  '(FREE %) — carrying 15 into a '
                                                                  'used-% rule would fire '
                                                                  'permanently',
                                                          'metric_class': 'capacity'},
                                'es_pending_tasks': {'value': 50,
                                                     'unit': 'count',
                                                     'desc': 'Pending cluster tasks warning; 50 = '
                                                             'existing de-facto shipped value '
                                                             '(init_project.py catalog + '
                                                             'examples/es-prod.yaml); #1196 D: key '
                                                             'was alert-demanded but absent from '
                                                             'scaffold',
                                                     'metric_class': 'state'},
                                'es_search_latency_ms': {'value': 1000,
                                                         'unit': 'ms',
                                                         'desc': 'Avg search latency warning '
                                                                 '(rate(query_time)/rate(query_total), '
                                                                 'per-query mean); 1000 = Awesome '
                                                                 'Prometheus Alerts '
                                                                 'ElasticsearchHighQueryLatency '
                                                                 'anchor (same formula, 1s/5m '
                                                                 'warning) — the only community '
                                                                 'rule semantically identical to '
                                                                 'ours; full derivation + '
                                                                 'calibration caveat in the pack '
                                                                 'header (#1196 D)',
                                                         'metric_class': 'latency'}},
                   'optional_overrides': {'es_disk_usage_percent_critical': {'value': 90,
                                                                             'unit': '%',
                                                                             'desc': 'Disk usage '
                                                                                     'critical',
                                                                             'metric_class': 'capacity'},
                                          'es_heap_usage_percent_critical': {'value': 90,
                                                                             'unit': '%',
                                                                             'desc': 'JVM heap '
                                                                                     'usage '
                                                                                     'critical',
                                                                             'metric_class': 'saturation'},
                                          'es_pending_tasks_critical': {'value': 150,
                                                                        'unit': 'count',
                                                                        'desc': 'Pending cluster '
                                                                                'tasks critical',
                                                                        'metric_class': 'state'},
                                          'es_search_latency_ms_critical': {'value': 2000,
                                                                            'unit': 'ms',
                                                                            'desc': 'Avg search '
                                                                                    'latency '
                                                                                    'critical',
                                                                            'metric_class': 'latency'}}},
 'oracle': {'display': 'Oracle Database (oracledb_exporter)',
            'exporter': 'iamseth/oracledb_exporter',
            'default_on': False,
            'rule_pack_file': 'rule-packs/rule-pack-oracle.yaml',
            'defaults': {'oracle_sessions_active': {'value': 200,
                                                    'unit': 'count',
                                                    'desc': 'Active sessions warning',
                                                    'metric_class': 'saturation',
                                                    'value_counterexample': {'issue': 1176,
                                                                             'direction': 'over_fire',
                                                                             'observed': 'a '
                                                                                         'healthy '
                                                                                         'diurnal '
                                                                                         'peak '
                                                                                         'reaches '
                                                                                         '240 '
                                                                                         'active '
                                                                                         'sessions',
                                                                             'observed_zh': '健康的日夜週期尖峰會到 '
                                                                                            '240 個 '
                                                                                            'active '
                                                                                            'session'}},
                         'oracle_tablespace_used_percent': {'value': 85,
                                                            'unit': '%',
                                                            'desc': 'Tablespace usage warning',
                                                            'metric_class': 'capacity'}},
            'optional_overrides': {'oracle_wait_time_rate': {'value': 50,
                                                             'unit': 's/s',
                                                             'desc': 'Wait time rate (5m)',
                                                             'metric_class': 'latency'},
                                   'oracle_process_count': {'value': 300,
                                                            'unit': 'count',
                                                            'desc': 'Active processes warning',
                                                            'metric_class': 'capacity',
                                                            'value_counterexample': {'issue': 1176,
                                                                                     'direction': 'over_fire',
                                                                                     'observed': 'a '
                                                                                                 'routine '
                                                                                                 'backup '
                                                                                                 'batch '
                                                                                                 'reaches '
                                                                                                 '560 '
                                                                                                 'processes',
                                                                                     'observed_zh': '例行備份批次會到 '
                                                                                                    '560 '
                                                                                                    '個 '
                                                                                                    'process'}},
                                   'oracle_pga_allocated_bytes': {'value': 4294967296,
                                                                  'unit': 'bytes (4GB)',
                                                                  'desc': 'PGA allocation warning',
                                                                  'metric_class': 'capacity',
                                                                  'value_counterexample': {'issue': 1176,
                                                                                           'direction': 'over_fire',
                                                                                           'observed': 'a '
                                                                                                       'planned '
                                                                                                       'stats-gather '
                                                                                                       'reaches '
                                                                                                       '22 '
                                                                                                       'GB '
                                                                                                       'PGA',
                                                                                           'observed_zh': '計畫性 '
                                                                                                          'stats-gather '
                                                                                                          '會用到 '
                                                                                                          '22 '
                                                                                                          'GB '
                                                                                                          'PGA'}},
                                   'oracle_sessions_active_critical': {'value': 280,
                                                                       'unit': 'count',
                                                                       'desc': 'Active sessions '
                                                                               'critical',
                                                                       'metric_class': 'saturation'},
                                   'oracle_tablespace_used_percent_critical': {'value': 95,
                                                                               'unit': '%',
                                                                               'desc': 'Tablespace '
                                                                                       'usage '
                                                                                       'critical',
                                                                               'metric_class': 'capacity'}}},
 'db2': {'display': 'IBM DB2 (ibm_db2_exporter)',
         'exporter': 'ibm_db2_exporter (community)',
         'default_on': False,
         'rule_pack_file': 'rule-packs/rule-pack-db2.yaml',
         'defaults': {'db2_connections_active': {'value': 200,
                                                 'unit': 'count',
                                                 'desc': 'Active connections warning',
                                                 'metric_class': 'saturation',
                                                 'value_counterexample': {'issue': 1176,
                                                                          'direction': 'over_fire',
                                                                          'observed': 'a peak-hour '
                                                                                      'workload '
                                                                                      'reaches 360 '
                                                                                      'connections',
                                                                          'observed_zh': '尖峰時段的工作負載會到 '
                                                                                         '360 '
                                                                                         '個連線'}},
                      'db2_bufferpool_hit_ratio': {'value': 0.95,
                                                   'unit': 'ratio',
                                                   'desc': 'Bufferpool hit ratio warning',
                                                   'metric_class': 'efficiency'},
                      'db2_lock_wait_time': {'value': 10,
                                             'unit': 's/s',
                                             'desc': 'Lock wait accumulation rate warning (rate '
                                                     "over 5m, summed across the tenant's "
                                                     'instances); 10 ≈ 6.7x the 1.5 normal level '
                                                     'both reference libraries agree on, below '
                                                     'both fault levels (25/60) — full derivation '
                                                     '+ calibration workflow in the pack header '
                                                     '(#1196 D)',
                                             'metric_class': 'latency',
                                             'value_counterexample': {'issue': 1176,
                                                                      'direction': 'over_fire',
                                                                      'observed': 'a benign batch '
                                                                                  'lock-wait '
                                                                                  'transient holds '
                                                                                  '18 s/s for 7 '
                                                                                  'min '
                                                                                  '(negative-db2.yaml)',
                                                                      'observed_zh': '良性批次的 '
                                                                                     'lock-wait '
                                                                                     '短暫維持 18 s/s '
                                                                                     '達 7 '
                                                                                     '分鐘（negative-db2.yaml）'}}},
         'optional_overrides': {'db2_log_usage_percent': {'value': 70,
                                                          'unit': '%',
                                                          'desc': 'Transaction log usage warning',
                                                          'metric_class': 'capacity'},
                                'db2_deadlock_rate': {'value': 5,
                                                      'unit': 'count/s',
                                                      'desc': 'Deadlock rate (5m)',
                                                      'metric_class': 'errors',
                                                      'value_counterexample': {'issue': 1176,
                                                                               'direction': 'under_fire',
                                                                               'observed': 'a real '
                                                                                           '~90/min '
                                                                                           'deadlock '
                                                                                           'storm '
                                                                                           'stays '
                                                                                           'under '
                                                                                           '5/s '
                                                                                           '(=300/min) '
                                                                                           'and is '
                                                                                           'missed',
                                                                               'observed_zh': '真實的 '
                                                                                              '~90/min '
                                                                                              'deadlock '
                                                                                              'storm '
                                                                                              '低於 '
                                                                                              '5/s（＝300/min），因此接不到'}},
                                'db2_tablespace_used_percent': {'value': 85,
                                                                'unit': '%',
                                                                'desc': 'Tablespace usage warning',
                                                                'metric_class': 'capacity'},
                                'db2_connections_active_critical': {'value': 400,
                                                                    'unit': 'count',
                                                                    'desc': 'Active connections '
                                                                            'critical',
                                                                    'metric_class': 'saturation'},
                                'db2_lock_wait_time_critical': {'value': 20,
                                                                'unit': 's/s',
                                                                'desc': 'Lock wait accumulation '
                                                                        'rate critical',
                                                                'metric_class': 'latency'}}},
 'clickhouse': {'display': 'ClickHouse (built-in /metrics)',
                'exporter': 'ClickHouse built-in Prometheus endpoint',
                'default_on': False,
                'rule_pack_file': 'rule-packs/rule-pack-clickhouse.yaml',
                'defaults': {'clickhouse_queries_rate': {'value': 500,
                                                         'unit': 'qps',
                                                         'desc': 'Query rate warning (5m)',
                                                         'metric_class': 'throughput'},
                             'clickhouse_active_connections': {'value': 200,
                                                               'unit': 'count',
                                                               'desc': 'Active TCP connections '
                                                                       'warning',
                                                               'metric_class': 'saturation'}},
                'optional_overrides': {'clickhouse_max_part_count': {'value': 300,
                                                                     'unit': 'count',
                                                                     'desc': 'Max part count per '
                                                                             'partition',
                                                                     'metric_class': 'capacity'},
                                       'clickhouse_replication_queue': {'value': 50,
                                                                        'unit': 'count',
                                                                        'desc': 'Replication queue '
                                                                                'size',
                                                                        'metric_class': 'replication'},
                                       'clickhouse_memory_tracking_bytes': {'value': 8589934592,
                                                                            'unit': 'bytes (8GB)',
                                                                            'desc': 'Memory '
                                                                                    'tracking '
                                                                                    'warning',
                                                                            'metric_class': 'capacity'},
                                       'clickhouse_active_connections_critical': {'value': 300,
                                                                                  'unit': 'count',
                                                                                  'desc': 'Active '
                                                                                          'TCP '
                                                                                          'connections '
                                                                                          'critical',
                                                                                  'metric_class': 'saturation'}}},
 'kafka': {'display': 'Apache Kafka (danielqsj/kafka-exporter)',
           'exporter': 'danielqsj/kafka-exporter',
           'default_on': False,
           'rule_pack_file': 'rule-packs/rule-pack-kafka.yaml',
           'defaults': {'kafka_consumer_lag': {'value': 1000,
                                               'unit': 'messages',
                                               'desc': 'Consumer lag warning',
                                               'metric_class': 'saturation'},
                        'kafka_under_replicated_partitions': {'value': 0,
                                                              'unit': 'count',
                                                              'desc': 'Under-replicated partitions '
                                                                      '(should be 0)',
                                                              'metric_class': 'replication'},
                        'kafka_broker_count': {'value': 3,
                                               'unit': 'count',
                                               'desc': 'Minimum broker count warning',
                                               'metric_class': 'state'},
                        'kafka_active_controllers': {'value': 1,
                                                     'unit': 'count',
                                                     'desc': 'Minimum active controllers (should '
                                                             'be 1)',
                                                     'metric_class': 'state'},
                        'kafka_request_rate': {'value': 10000,
                                               'unit': 'msg/s',
                                               'desc': 'Message rate warning (5m)',
                                               'metric_class': 'throughput'}},
           'optional_overrides': {'kafka_consumer_lag_critical': {'value': 10000,
                                                                  'unit': 'messages',
                                                                  'desc': 'Consumer lag critical',
                                                                  'metric_class': 'saturation'},
                                  'kafka_under_replicated_partitions_critical': {'value': 1,
                                                                                 'unit': 'count',
                                                                                 'desc': 'Under-replicated '
                                                                                         'critical',
                                                                                 'metric_class': 'replication'},
                                  'kafka_request_rate_critical': {'value': 50000,
                                                                  'unit': 'msg/s',
                                                                  'desc': 'Message rate critical',
                                                                  'metric_class': 'throughput'}}},
 'rabbitmq': {'display': 'RabbitMQ (kbudde/rabbitmq_exporter)',
              'exporter': 'kbudde/rabbitmq_exporter or built-in Prometheus plugin',
              'default_on': False,
              'rule_pack_file': 'rule-packs/rule-pack-rabbitmq.yaml',
              'defaults': {'rabbitmq_queue_messages': {'value': 100000,
                                                       'unit': 'messages',
                                                       'desc': 'Queue depth warning',
                                                       'metric_class': 'saturation'},
                           'rabbitmq_node_mem_percent': {'value': 80,
                                                         'unit': '%',
                                                         'desc': 'Node memory usage warning',
                                                         'metric_class': 'saturation'},
                           'rabbitmq_connections': {'value': 1000,
                                                    'unit': 'count',
                                                    'desc': 'Connection count warning',
                                                    'metric_class': 'saturation'},
                           'rabbitmq_consumers': {'value': 5,
                                                  'unit': 'count',
                                                  'desc': 'Minimum consumer count warning',
                                                  'metric_class': 'state'},
                           'rabbitmq_unacked_messages': {'value': 10000,
                                                         'unit': 'messages',
                                                         'desc': 'Unacknowledged messages warning',
                                                         'metric_class': 'saturation'}},
              'optional_overrides': {'rabbitmq_queue_messages_critical': {'value': 500000,
                                                                          'unit': 'messages',
                                                                          'desc': 'Queue depth '
                                                                                  'critical',
                                                                          'metric_class': 'saturation'},
                                     'rabbitmq_node_mem_percent_critical': {'value': 95,
                                                                            'unit': '%',
                                                                            'desc': 'Node memory '
                                                                                    'critical',
                                                                            'metric_class': 'saturation'},
                                     'rabbitmq_connections_critical': {'value': 1500,
                                                                       'unit': 'count',
                                                                       'desc': 'Connection count '
                                                                               'critical',
                                                                       'metric_class': 'saturation'},
                                     'rabbitmq_unacked_messages_critical': {'value': 50000,
                                                                            'unit': 'messages',
                                                                            'desc': 'Unacknowledged '
                                                                                    'messages '
                                                                                    'critical',
                                                                            'metric_class': 'saturation'}}},
 'jvm': {'display': 'JVM (jmx_exporter / Micrometer)',
         'exporter': 'prometheus/jmx_exporter or Spring Boot Actuator',
         'default_on': False,
         'rule_pack_file': 'rule-packs/rule-pack-jvm.yaml',
         'defaults': {'jvm_gc_pause': {'value': 0.5,
                                       'unit': 'seconds/5m',
                                       'desc': 'GC pause duration rate warning',
                                       'metric_class': 'latency'},
                      'jvm_memory': {'value': 80,
                                     'unit': '%',
                                     'desc': 'Heap memory usage warning',
                                     'metric_class': 'saturation'},
                      'jvm_threads': {'value': 500,
                                      'unit': 'count',
                                      'desc': 'Active thread count warning',
                                      'metric_class': 'saturation'}},
         'optional_overrides': {'jvm_gc_pause_critical': {'value': 1.0,
                                                          'unit': 'seconds/5m',
                                                          'desc': 'GC pause critical',
                                                          'metric_class': 'latency'},
                                'jvm_memory_critical': {'value': 95,
                                                        'unit': '%',
                                                        'desc': 'Heap memory critical',
                                                        'metric_class': 'saturation'},
                                'jvm_threads_critical': {'value': 800,
                                                         'unit': 'count',
                                                         'desc': 'Thread count critical',
                                                         'metric_class': 'saturation'}}},
 'nginx': {'display': 'Nginx (nginx-prometheus-exporter)',
           'exporter': 'nginxinc/nginx-prometheus-exporter',
           'default_on': False,
           'rule_pack_file': 'rule-packs/rule-pack-nginx.yaml',
           'defaults': {'nginx_connections': {'value': 1000,
                                              'unit': 'count',
                                              'desc': 'Active connections warning',
                                              'metric_class': 'saturation'},
                        'nginx_request_rate': {'value': 5000,
                                               'unit': 'req/s',
                                               'desc': 'Request rate warning',
                                               'metric_class': 'throughput'},
                        'nginx_waiting': {'value': 200,
                                          'unit': 'count',
                                          'desc': 'Waiting connections (backlog) warning',
                                          'metric_class': 'saturation'}},
           'optional_overrides': {'nginx_connections_critical': {'value': 2000,
                                                                 'unit': 'count',
                                                                 'desc': 'Active connections '
                                                                         'critical',
                                                                 'metric_class': 'saturation'},
                                  'nginx_request_rate_critical': {'value': 10000,
                                                                  'unit': 'req/s',
                                                                  'desc': 'Request rate critical',
                                                                  'metric_class': 'throughput'},
                                  'nginx_waiting_critical': {'value': 500,
                                                             'unit': 'count',
                                                             'desc': 'Waiting connections critical',
                                                             'metric_class': 'saturation'}}}}
