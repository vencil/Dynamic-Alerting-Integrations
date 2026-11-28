"""GENERATED from rule-packs/threshold-registry.yaml — DO NOT EDIT.

The threshold half of scaffold_tenant.RULE_PACKS (D-A PR-3): per pack its
display/exporter/default_on/rule_pack_file(/queries/presence/metric_prefixes/
undeclared_notice/exporters/unchecked_alternatives)
and its `thresholds` (registry key order), BUILTIN_DEFAULT_KEYS: the keys the
readers build in with a platform value (`arming: default`, the same derivation
as builtin_defaults_gen.go, #3832), and EXPECTED_EXPORTER_KINDS: the packs with
`presence:`, the kinds a tenant may name in `_expected_exporters` (#4097).
Edit the registry, then run
  python3 scripts/tools/lint/check_threshold_registry.py --regen
`--ci` (pre-commit, CI) fails while this file is stale.
"""

THRESHOLD_PACKS = {'kubernetes': {'display': 'Kubernetes (cAdvisor + KSM)',
                'exporter': 'cAdvisor + kube-state-metrics',
                'default_on': True,
                'rule_pack_file': 'rule-packs/rule-pack-kubernetes.yaml',
                'metric_prefixes': ['container_', 'kube_', 'kubelet_'],
                'thresholds': {'container_cpu': {'value': 80,
                                                 'unit': '%',
                                                 'desc': 'Container CPU % of limit (weakest link)',
                                                 'metric_class': 'saturation',
                                                 'dimensions': ['version']},
                               'container_cpu_throttle': {'value': 25,
                                                          'unit': '%',
                                                          'desc': 'Chronic CFS throttle: % of '
                                                                  'ACTIVE 100ms periods throttled, '
                                                                  'NOT % CPU lost (critical tier '
                                                                  'opt-in via '
                                                                  'container_cpu_throttle_critical; '
                                                                  '#944)',
                                                          'metric_class': 'saturation'},
                               'container_memory': {'value': 85,
                                                    'unit': '%',
                                                    'desc': 'Container memory % of limit (weakest '
                                                            'link)',
                                                    'metric_class': 'saturation',
                                                    'dimensions': ['version']},
                               'container_cpu_critical': {'value': 95,
                                                          'unit': '%',
                                                          'desc': 'Container CPU critical',
                                                          'metric_class': 'saturation'},
                               'container_cpu_throttle_critical': {'value': 50,
                                                                   'unit': '%',
                                                                   'desc': 'CFS throttle critical',
                                                                   'metric_class': 'saturation'},
                               'container_memory_critical': {'value': 95,
                                                             'unit': '%',
                                                             'desc': 'Container memory critical',
                                                             'metric_class': 'saturation'}}},
 'postgresql': {'display': 'PostgreSQL (postgres_exporter)',
                'exporter': 'prometheus-community/postgres_exporter',
                'default_on': False,
                'rule_pack_file': 'rule-packs/rule-pack-postgresql.yaml',
                'presence': ['pg_up'],
                'metric_prefixes': ['pg_'],
                'exporters': [{'name': 'prometheus-community/postgres_exporter',
                               'image': 'quay.io/prometheuscommunity/postgres-exporter',
                               'tag': 'v0.21.1',
                               'digest': 'sha256:a88794758ad461c75f3825b51cde4bde730bdff04b3b24d00efe14217e41f615',
                               'flags': ['--collector.postmaster'],
                               'server': {'image': 'postgres',
                                          'tag': '16.15',
                                          'digest': 'sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657'},
                               'states': ['primary', 'replica']}],
                'unchecked_alternatives': [{'metric': 'pg_replication_lag',
                                            'reason': 'postgres_exporter v0.12.1 以前的名稱，來自它範例 '
                                                      'queries.yaml 的 pg_replication 項目 '
                                                      '（--extend.query-path）；v0.13.0 起由 '
                                                      'replication collector 改供應 '
                                                      'pg_replication_lag_seconds。保留給還在用那份查詢檔的租戶，釘住的版本不供應這個名稱'}],
                'thresholds': {'pg_connections': {'value': 80,
                                                  'unit': '% of max_connections',
                                                  'desc': 'Connection usage % warning',
                                                  'metric_class': 'saturation'},
                               'pg_replication_lag': {'value': 30,
                                                      'unit': 'seconds',
                                                      'desc': 'Replication lag warning',
                                                      'metric_class': 'replication'},
                               'pg_connections_critical': {'value': 90,
                                                           'unit': '% of max_connections',
                                                           'desc': 'Critical tier connections',
                                                           'metric_class': 'saturation'},
                               'pg_replication_lag_critical': {'value': 60,
                                                               'unit': 'seconds',
                                                               'desc': 'Critical tier replication '
                                                                       'lag',
                                                               'metric_class': 'replication'}}},
 'mariadb': {'display': 'MariaDB / MySQL (mysqld_exporter)',
             'exporter': 'prom/mysqld-exporter or Percona',
             'default_on': True,
             'rule_pack_file': 'rule-packs/rule-pack-mariadb.yaml',
             'presence': ['mysql_up'],
             'metric_prefixes': ['mysql_'],
             'thresholds': {'mysql_connection_usage_percent': {'value': 80,
                                                               'unit': '% of max_connections',
                                                               'desc': 'Connection usage warning: '
                                                                       'threads_connected as a '
                                                                       'share of max_connections, '
                                                                       'per instance, busiest '
                                                                       'instance per tenant; 80 = '
                                                                       'PMM "MySQL connections in '
                                                                       'use" template / '
                                                                       'awesome-prometheus-alerts. '
                                                                       'Replaces the '
                                                                       'absolute-count key '
                                                                       'mysql_connections (#3778: '
                                                                       'a unit change is a removal '
                                                                       'plus an addition, no '
                                                                       'alias)',
                                                               'metric_class': 'saturation'},
                            'mysql_threads_running': {'value': 30,
                                                      'unit': 'threads',
                                                      'desc': 'threads_running saturation, NOT '
                                                              'host CPU% — running-threads 1m-avg '
                                                              "warning; 30 = PMM/Nichter 'high' "
                                                              'tier, a reference start to '
                                                              'calibrate per host (a healthy count '
                                                              'scales with CPU cores); renamed '
                                                              'from mysql_cpu (2-release alias '
                                                              'window, old spelling still '
                                                              'resolves)',
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
                                                      'metric_class': 'replication'},
                            'mysql_connection_usage_percent_critical': {'value': 90,
                                                                        'unit': '% of '
                                                                                'max_connections',
                                                                        'desc': 'Critical tier '
                                                                                'connection usage '
                                                                                '(same tier pair '
                                                                                'as '
                                                                                'pg_connections_critical)',
                                                                        'metric_class': 'saturation'},
                            'mysql_threads_running_critical': {'value': 50,
                                                               'unit': 'threads',
                                                               'desc': 'Critical tier '
                                                                       '(running-threads '
                                                                       'saturation); 120→50 = PMM '
                                                                       'critical-load / Nichter '
                                                                       "'overloaded' (#944); "
                                                                       'renamed from '
                                                                       'mysql_cpu_critical (#1231, '
                                                                       '2-release alias window)',
                                                               'metric_class': 'saturation'},
                            'mysql_replication_lag_critical': {'value': 120,
                                                               'unit': 'seconds',
                                                               'desc': 'Critical tier replication '
                                                                       'lag; 120 = 4x the '
                                                                       'APA/mixin 30s trigger, 5x '
                                                                       'below PMM family 600s — '
                                                                       'sustained minutes-level '
                                                                       'lag is a failover '
                                                                       'data-loss window (#1200 '
                                                                       'WS5-P0-a)',
                                                               'metric_class': 'replication'}}},
 'redis': {'display': 'Redis (oliver006/redis_exporter)',
           'exporter': 'oliver006/redis_exporter',
           'default_on': False,
           'rule_pack_file': 'rule-packs/rule-pack-redis.yaml',
           'presence': ['redis_up'],
           'metric_prefixes': ['redis_'],
           'thresholds': {'redis_memory_used_bytes': {'value': 4294967296,
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
                                                              'optional_overrides→defaults — '
                                                              'unwired dead key, no alias',
                                                      'metric_class': 'efficiency'},
                          'redis_replication_lag': {'value': 30,
                                                    'unit': 'seconds',
                                                    'desc': 'Replica lag warning; 30 = same anchor '
                                                            'as mysql/pg replication_lag — redis '
                                                            'replication is normally sub-second, '
                                                            'so 30s is unambiguous trouble '
                                                            '(noise-averse pick, #1231 owner '
                                                            'call); #1196 C identity fix, no alias',
                                                    'metric_class': 'replication'},
                          'redis_connected_clients_critical': {'value': 300,
                                                               'unit': 'count',
                                                               'desc': 'Connected clients critical',
                                                               'metric_class': 'saturation'},
                          'redis_memory_used_bytes_critical': {'value': 5368709120,
                                                               'unit': 'bytes',
                                                               'desc': 'Memory usage critical',
                                                               'metric_class': 'saturation'},
                          'redis_replication_lag_critical': {'value': 50,
                                                             'unit': 'seconds',
                                                             'desc': 'Replica lag critical',
                                                             'metric_class': 'replication'}}},
 'mongodb': {'display': 'MongoDB (percona/mongodb_exporter)',
             'exporter': 'percona/mongodb_exporter',
             'default_on': False,
             'rule_pack_file': 'rule-packs/rule-pack-mongodb.yaml',
             'presence': ['mongodb_up'],
             'metric_prefixes': ['mongodb_'],
             'thresholds': {'mongodb_connections_current': {'value': 300,
                                                            'unit': 'count',
                                                            'desc': 'Current connections warning',
                                                            'metric_class': 'saturation'},
                            'mongodb_replication_lag': {'value': 10,
                                                        'unit': 'seconds',
                                                        'desc': 'Replication lag warning; #1196 C '
                                                                'identity fix: renamed from '
                                                                'mongodb_repl_lag_seconds (alert '
                                                                'demands mongodb_replication_lag) '
                                                                '— unwired dead key, no alias',
                                                        'metric_class': 'replication'},
                            'mongodb_opcounters_rate': {'value': 10000,
                                                        'unit': 'ops/s',
                                                        'desc': 'Total operations rate; #1196 B '
                                                                'identity fix: renamed from '
                                                                'mongodb_opcounters_total (alert '
                                                                'demands _rate) + moved '
                                                                'optional_overrides→defaults — '
                                                                'unwired dead key, no alias',
                                                        'metric_class': 'throughput'},
                            'mongodb_connections_current_critical': {'value': 450,
                                                                     'unit': 'count',
                                                                     'desc': 'Current connections '
                                                                             'critical',
                                                                     'metric_class': 'saturation'},
                            'mongodb_replication_lag_critical': {'value': 30,
                                                                 'unit': 'seconds',
                                                                 'desc': 'Replication lag critical',
                                                                 'metric_class': 'replication'}}},
 'elasticsearch': {'display': 'Elasticsearch (elasticsearch_exporter)',
                   'exporter': 'justwatchcom/elasticsearch_exporter',
                   'default_on': False,
                   'rule_pack_file': 'rule-packs/rule-pack-elasticsearch.yaml',
                   'presence': ['elasticsearch_exporter_build_info'],
                   'metric_prefixes': ['elasticsearch_'],
                   'thresholds': {'es_heap_usage_percent': {'value': 85,
                                                            'unit': '%',
                                                            'desc': 'JVM heap usage warning; #1196 '
                                                                    'C identity fix: renamed from '
                                                                    'es_jvm_memory_used_percent '
                                                                    '(alert demands '
                                                                    'es_heap_usage_percent) — '
                                                                    'unwired dead key, no alias',
                                                            'metric_class': 'saturation'},
                                  'es_disk_usage_percent': {'value': 85,
                                                            'unit': '%',
                                                            'desc': 'Disk USAGE % warning (used = '
                                                                    '1 - available/size, recording '
                                                                    'rule '
                                                                    'tenant:es_disk_usage_percent:max, '
                                                                    'compared with >); 85 = same '
                                                                    'band as oracle/db2 '
                                                                    'tablespace_used_percent; '
                                                                    '#1196 C semantic flip: '
                                                                    'replaces '
                                                                    'es_filesystem_free_percent 15 '
                                                                    '(FREE %) — carrying 15 into a '
                                                                    'used-% rule would fire '
                                                                    'permanently',
                                                            'metric_class': 'capacity'},
                                  'es_pending_tasks': {'value': 50,
                                                       'unit': 'count',
                                                       'desc': 'Pending cluster tasks warning; 50 '
                                                               '= existing de-facto shipped value '
                                                               '(init_project.py catalog + '
                                                               'examples/es-prod.yaml); #1196 D: '
                                                               'key was alert-demanded but absent '
                                                               'from scaffold',
                                                       'metric_class': 'state'},
                                  'es_search_latency_ms': {'value': 1000,
                                                           'unit': 'ms',
                                                           'desc': 'Avg search latency warning '
                                                                   '(rate(query_time)/rate(query_total), '
                                                                   'per-query mean); 1000 = '
                                                                   'Awesome Prometheus Alerts '
                                                                   'ElasticsearchHighQueryLatency '
                                                                   'anchor (same formula, 1s/5m '
                                                                   'warning) — the only community '
                                                                   'rule semantically identical to '
                                                                   'ours; full derivation + '
                                                                   'calibration caveat in the pack '
                                                                   'header (#1196 D)',
                                                           'metric_class': 'latency'},
                                  'es_disk_usage_percent_critical': {'value': 90,
                                                                     'unit': '%',
                                                                     'desc': 'Disk usage critical',
                                                                     'metric_class': 'capacity'},
                                  'es_heap_usage_percent_critical': {'value': 90,
                                                                     'unit': '%',
                                                                     'desc': 'JVM heap usage '
                                                                             'critical',
                                                                     'metric_class': 'saturation'},
                                  'es_pending_tasks_critical': {'value': 150,
                                                                'unit': 'count',
                                                                'desc': 'Pending cluster tasks '
                                                                        'critical',
                                                                'metric_class': 'state'},
                                  'es_search_latency_ms_critical': {'value': 2000,
                                                                    'unit': 'ms',
                                                                    'desc': 'Avg search latency '
                                                                            'critical',
                                                                    'metric_class': 'latency'}}},
 'oracle': {'display': 'Oracle Database (query-exporter)',
            'exporter': 'albertodonato/query-exporter',
            'default_on': False,
            'rule_pack_file': 'rule-packs/rule-pack-oracle.yaml',
            'queries': 'rule-packs/exporters/oracle.yaml',
            'presence': ['query_interval{query="oracle_liveness"}'],
            'metric_prefixes': ['oracledb_'],
            'thresholds': {'oracle_sessions_active': {'value': 200,
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
                                                                                              '240 '
                                                                                              '個 '
                                                                                              'active '
                                                                                              'session'}},
                           'oracle_tablespace_used_percent': {'value': 85,
                                                              'unit': '%',
                                                              'desc': 'Tablespace usage warning',
                                                              'metric_class': 'capacity'},
                           'oracle_wait_time_rate': {'value': 50,
                                                     'unit': 's/s',
                                                     'desc': 'Wait time rate (5m)',
                                                     'metric_class': 'latency'},
                           'oracle_hard_parse_time_percent': {'value': 20,
                                                              'unit': '% of DB time',
                                                              'desc': 'Hard parse time warning: '
                                                                      'v$sys_time_model "hard '
                                                                      'parse elapsed time" as a '
                                                                      'share of "DB time" over 5m '
                                                                      '(the AWR Time Model "% of '
                                                                      'DB Time" row), per '
                                                                      'exporter, busiest per '
                                                                      'tenant; only while the '
                                                                      'database averages at least '
                                                                      'one active session (AAS >= '
                                                                      '1)',
                                                              'metric_class': 'latency'},
                           'oracle_process_usage_percent': {'value': 85,
                                                            'unit': '% of PROCESSES',
                                                            'desc': 'Process usage warning: '
                                                                    'v$process rows as a share of '
                                                                    'the PROCESSES parameter, per '
                                                                    'exporter, busiest per tenant; '
                                                                    '85 = '
                                                                    'awesome-prometheus-alerts '
                                                                    '"Oracle DB processes reaching '
                                                                    'limit". Replaces the '
                                                                    'absolute-count key '
                                                                    'oracle_process_count (#1320: '
                                                                    'a unit change is a removal '
                                                                    'plus an addition, no alias)',
                                                            'metric_class': 'capacity'},
                           'oracle_pga_usage_percent': {'value': 85,
                                                        'unit': '% of PGA_AGGREGATE_LIMIT',
                                                        'desc': 'PGA usage warning: v$pgastat '
                                                                'total PGA allocated as a share of '
                                                                'PGA_AGGREGATE_LIMIT (past it '
                                                                'Oracle terminates sessions), per '
                                                                'exporter, busiest per tenant; a '
                                                                'database with no limit set (0) '
                                                                'has no share. Replaces the '
                                                                'absolute-bytes key '
                                                                'oracle_pga_allocated_bytes '
                                                                '(#1320: a unit change is a '
                                                                'removal plus an addition, no '
                                                                'alias)',
                                                        'metric_class': 'capacity'},
                           'oracle_sessions_active_critical': {'value': 280,
                                                               'unit': 'count',
                                                               'desc': 'Active sessions critical',
                                                               'metric_class': 'saturation'},
                           'oracle_tablespace_used_percent_critical': {'value': 95,
                                                                       'unit': '%',
                                                                       'desc': 'Tablespace usage '
                                                                               'critical',
                                                                       'metric_class': 'capacity'}}},
 'db2': {'display': 'IBM DB2 (query-exporter)',
         'exporter': 'albertodonato/query-exporter',
         'default_on': False,
         'rule_pack_file': 'rule-packs/rule-pack-db2.yaml',
         'queries': 'rule-packs/exporters/db2.yaml',
         'presence': ['query_interval{query="db2_liveness"}'],
         'metric_prefixes': ['db2_'],
         'thresholds': {'db2_connections_active': {'value': 200,
                                                   'unit': 'count',
                                                   'desc': 'Active connections warning',
                                                   'metric_class': 'saturation',
                                                   'value_counterexample': {'issue': 1176,
                                                                            'direction': 'over_fire',
                                                                            'observed': 'a '
                                                                                        'peak-hour '
                                                                                        'workload '
                                                                                        'reaches '
                                                                                        '360 '
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
                                                       'both fault levels (25/60) — full '
                                                       'derivation + calibration workflow in the '
                                                       'pack header (#1196 D)',
                                               'metric_class': 'latency',
                                               'value_counterexample': {'issue': 1176,
                                                                        'direction': 'over_fire',
                                                                        'observed': 'a benign '
                                                                                    'batch '
                                                                                    'lock-wait '
                                                                                    'transient '
                                                                                    'holds 18 s/s '
                                                                                    'for 7 min '
                                                                                    '(negative-db2.yaml)',
                                                                        'observed_zh': '良性批次的 '
                                                                                       'lock-wait '
                                                                                       '短暫維持 18 '
                                                                                       's/s 達 7 '
                                                                                       '分鐘（negative-db2.yaml）'}},
                        'db2_log_usage_percent': {'value': 75,
                                                  'unit': '%',
                                                  'desc': 'Transaction log usage warning: used '
                                                          'over used plus available active log '
                                                          'space, fullest database per tenant; 75 '
                                                          "= the warning value IBM's GET ALERT "
                                                          'CONFIGURATION page shows as the default '
                                                          'of the db.log_util health indicator, '
                                                          'which has the same formula (the health '
                                                          'monitor itself is deprecated; the '
                                                          'number is what is borrowed)',
                                                  'metric_class': 'capacity'},
                        'db2_deadlocks_per_5m': {'value': 5,
                                                 'unit': 'count/5m',
                                                 'desc': 'Deadlocks in the last 5 minutes, summed '
                                                         "over the tenant's exporters; 5 is the "
                                                         "threshold of the grafana ibm-db2 mixin's "
                                                         'IBMDB2HighNumberOfDeadlocks (increase '
                                                         'over 5m > 5, per database, for 5m). '
                                                         'Replaces the per-second key '
                                                         'db2_deadlock_rate (#1320: a unit change '
                                                         'is a removal plus an addition, no alias)',
                                                 'metric_class': 'errors',
                                                 'value_counterexample': {'issue': 1176,
                                                                          'direction': 'over_fire',
                                                                          'observed': 'one '
                                                                                      'reference '
                                                                                      "library's "
                                                                                      'normal '
                                                                                      'level is '
                                                                                      'about 6 '
                                                                                      'deadlocks '
                                                                                      'per 5 '
                                                                                      'minutes '
                                                                                      '(0.02/s), '
                                                                                      'above 5',
                                                                          'observed_zh': '其中一個參考庫的正常水位約每 '
                                                                                         '5 分鐘 6 次 '
                                                                                         'deadlock（0.02/s），高於 '
                                                                                         '5'}},
                        'db2_tablespace_used_percent': {'value': 80,
                                                        'unit': '%',
                                                        'desc': 'Tablespace usage warning: share '
                                                                'of the space a tablespace can '
                                                                'grow to, fullest tablespace per '
                                                                'tenant; 80 = the default warning '
                                                                'threshold IBM states for the '
                                                                'ts.ts_util health indicator (used '
                                                                'over usable pages — the form '
                                                                'served for a tablespace that does '
                                                                'not auto-resize; that indicator '
                                                                'is not evaluated for an '
                                                                'auto-resizing one, and the pages '
                                                                'cited state no default for the '
                                                                'auto-resize form, which reuses 80 '
                                                                'here). The health monitor itself '
                                                                'is deprecated; the number is what '
                                                                'is borrowed',
                                                        'metric_class': 'capacity'},
                        'db2_storage_path_used_percent': {'value': 80,
                                                          'unit': '%',
                                                          'desc': 'Storage path file system usage '
                                                                  'warning: bytes used over the '
                                                                  'size of the file system an '
                                                                  'automatic-storage path is on, '
                                                                  'fullest path per tenant — what '
                                                                  'a tablespace with no MAXSIZE '
                                                                  'runs out of (#4093). 80 is the '
                                                                  "warning threshold of Zabbix's "
                                                                  'Linux template and equals '
                                                                  'db2_tablespace_used_percent; '
                                                                  "Db2's own indicator for the LOG "
                                                                  'file system (db.log_fs_util, '
                                                                  'the same used / total) warns at '
                                                                  '75 — Db2 publishes none for '
                                                                  'storage paths. Zabbix leaves '
                                                                  'root-reserved blocks out of its '
                                                                  'total, so its figure reads a '
                                                                  'few points higher than this '
                                                                  'one. The file system may hold '
                                                                  'other data too',
                                                          'metric_class': 'capacity'},
                        'db2_connections_active_critical': {'value': 400,
                                                            'unit': 'count',
                                                            'desc': 'Active connections critical',
                                                            'metric_class': 'saturation'},
                        'db2_lock_wait_time_critical': {'value': 20,
                                                        'unit': 's/s',
                                                        'desc': 'Lock wait accumulation rate '
                                                                'critical',
                                                        'metric_class': 'latency'}}},
 'clickhouse': {'display': 'ClickHouse (built-in /metrics)',
                'exporter': 'ClickHouse built-in Prometheus endpoint',
                'default_on': False,
                'rule_pack_file': 'rule-packs/rule-pack-clickhouse.yaml',
                'presence': ['ClickHouseMetrics_TCPConnection'],
                'metric_prefixes': ['ClickHouse'],
                'thresholds': {'clickhouse_queries_rate': {'value': 500,
                                                           'unit': 'qps',
                                                           'desc': 'Query rate warning (5m)',
                                                           'metric_class': 'throughput'},
                               'clickhouse_active_connections': {'value': 200,
                                                                 'unit': 'count',
                                                                 'desc': 'Active TCP connections '
                                                                         'warning',
                                                                 'metric_class': 'saturation'},
                               'clickhouse_max_part_count': {'value': 300,
                                                             'unit': 'count',
                                                             'desc': 'Max part count per partition',
                                                             'metric_class': 'capacity'},
                               'clickhouse_replication_queue': {'value': 50,
                                                                'unit': 'count',
                                                                'desc': 'Replication queue size',
                                                                'metric_class': 'replication'},
                               'clickhouse_active_connections_critical': {'value': 300,
                                                                          'unit': 'count',
                                                                          'desc': 'Active TCP '
                                                                                  'connections '
                                                                                  'critical',
                                                                          'metric_class': 'saturation'}}},
 'kafka': {'display': 'Apache Kafka (danielqsj/kafka-exporter)',
           'exporter': 'danielqsj/kafka-exporter',
           'default_on': False,
           'rule_pack_file': 'rule-packs/rule-pack-kafka.yaml',
           'presence': ['kafka_brokers'],
           'metric_prefixes': ['kafka_'],
           'thresholds': {'kafka_consumer_lag': {'value': 1000,
                                                 'unit': 'messages',
                                                 'desc': 'Consumer lag warning',
                                                 'metric_class': 'saturation'},
                          'kafka_under_replicated_partitions': {'value': 0,
                                                                'unit': 'count',
                                                                'desc': 'Under-replicated '
                                                                        'partitions (should be 0)',
                                                                'metric_class': 'replication'},
                          'kafka_broker_count': {'value': 3,
                                                 'unit': 'count',
                                                 'desc': 'Minimum broker count warning',
                                                 'metric_class': 'state'},
                          'kafka_consumer_lag_critical': {'value': 10000,
                                                          'unit': 'messages',
                                                          'desc': 'Consumer lag critical',
                                                          'metric_class': 'saturation'},
                          'kafka_under_replicated_partitions_critical': {'value': 1,
                                                                         'unit': 'count',
                                                                         'desc': 'Under-replicated '
                                                                                 'critical',
                                                                         'metric_class': 'replication'}}},
 'rabbitmq': {'display': 'RabbitMQ (kbudde/rabbitmq_exporter)',
              'exporter': 'kbudde/rabbitmq_exporter or built-in Prometheus plugin',
              'default_on': False,
              'rule_pack_file': 'rule-packs/rule-pack-rabbitmq.yaml',
              'presence': ['rabbitmq_up', 'rabbitmq_identity_info'],
              'metric_prefixes': ['rabbitmq_'],
              'thresholds': {'rabbitmq_queue_messages': {'value': 100000,
                                                         'unit': 'messages',
                                                         'desc': 'Queue depth warning',
                                                         'metric_class': 'saturation'},
                             'rabbitmq_node_mem_percent': {'value': 90,
                                                           'unit': '%',
                                                           'desc': 'Node memory warning: resident '
                                                                   'memory as % of the memory high '
                                                                   'watermark, fullest node '
                                                                   '(#3929: 80 → 90, measured per '
                                                                   'node)',
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
                                                           'desc': 'Unacknowledged messages '
                                                                   'warning',
                                                           'metric_class': 'saturation'},
                             'rabbitmq_queue_messages_critical': {'value': 500000,
                                                                  'unit': 'messages',
                                                                  'desc': 'Queue depth critical',
                                                                  'metric_class': 'saturation'},
                             'rabbitmq_node_mem_percent_critical': {'value': 95,
                                                                    'unit': '%',
                                                                    'desc': 'Node memory critical',
                                                                    'metric_class': 'saturation'},
                             'rabbitmq_connections_critical': {'value': 1500,
                                                               'unit': 'count',
                                                               'desc': 'Connection count critical',
                                                               'metric_class': 'saturation'},
                             'rabbitmq_unacked_messages_critical': {'value': 50000,
                                                                    'unit': 'messages',
                                                                    'desc': 'Unacknowledged '
                                                                            'messages critical',
                                                                    'metric_class': 'saturation'}}},
 'jvm': {'display': 'JVM (Micrometer / client_java / jmx_exporter)',
         'exporter': 'Micrometer (Spring Boot Actuator), prometheus/client_java or '
                     'prometheus/jmx_exporter',
         'default_on': False,
         'rule_pack_file': 'rule-packs/rule-pack-jvm.yaml',
         'presence': ['jvm_memory_used_bytes', 'jvm_memory_bytes_used'],
         'metric_prefixes': ['jvm_'],
         'undeclared_notice': False,
         'thresholds': {'jvm_gc_time_percent': {'value': 10,
                                                'unit': '% of time',
                                                'desc': 'GC time warning: % of wall-clock time the '
                                                        'busiest JVM of the tenant (per cluster) '
                                                        'spends in GC pauses, averaged over 5m '
                                                        '(#4116). Per JVM, never summed over JVMs: '
                                                        'ten JVMs at 7.5% each read 7.5',
                                                'metric_class': 'latency'},
                        'jvm_memory': {'value': 80,
                                       'unit': '%',
                                       'desc': 'Heap memory warning: the whole heap as % of its '
                                               'max, fullest JVM (#3928: pools summed before '
                                               'dividing)',
                                       'metric_class': 'saturation'},
                        'jvm_threads': {'value': 500,
                                        'unit': 'count',
                                        'desc': 'Active thread count warning',
                                        'metric_class': 'saturation'},
                        'jvm_gc_time_percent_critical': {'value': 25,
                                                         'unit': '% of time',
                                                         'desc': 'GC time critical: % of '
                                                                 'wall-clock time the busiest JVM '
                                                                 'spends in GC pauses (see '
                                                                 'jvm_gc_time_percent)',
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
           'presence': ['nginx_up'],
           'metric_prefixes': ['nginx_'],
           'thresholds': {'nginx_connections': {'value': 1000,
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
                                            'metric_class': 'saturation'},
                          'nginx_connections_critical': {'value': 2000,
                                                         'unit': 'count',
                                                         'desc': 'Active connections critical',
                                                         'metric_class': 'saturation'},
                          'nginx_request_rate_critical': {'value': 10000,
                                                          'unit': 'req/s',
                                                          'desc': 'Request rate critical',
                                                          'metric_class': 'throughput'},
                          'nginx_waiting_critical': {'value': 500,
                                                     'unit': 'count',
                                                     'desc': 'Waiting connections critical',
                                                     'metric_class': 'saturation'}}}}

BUILTIN_DEFAULT_KEYS = frozenset({'container_cpu', 'container_cpu_throttle', 'container_memory', 'db2_log_usage_percent', 'db2_storage_path_used_percent', 'db2_tablespace_used_percent', 'es_disk_usage_percent', 'es_heap_usage_percent', 'mysql_connection_usage_percent', 'mysql_replication_lag', 'oracle_process_usage_percent', 'oracle_tablespace_used_percent', 'pg_connections', 'pg_replication_lag', 'rabbitmq_node_mem_percent'})

EXPECTED_EXPORTER_KINDS = ('clickhouse', 'db2', 'elasticsearch', 'jvm', 'kafka', 'mariadb', 'mongodb', 'nginx', 'oracle', 'postgresql', 'rabbitmq', 'redis',)
