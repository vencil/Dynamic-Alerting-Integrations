package main

// fixtureDims is the `dimensions:` declaration (#3475) the da-guard fixtures'
// root defaults carry so their dimensional keys are served: a dimensional key
// is served only when every label of its selector is declared for its base.
// Some fixtures deliberately carry labels the collector drops or rejects
// (tenant, severity, q_re, a reserved name): declared so the row reaches
// the reader those fixtures test. Prepend it to a root `_defaults.yaml` text (it is a top-level block).
const fixtureDims = "dimensions:\n" +
	"  redis_queue_length: [a, a_re, priority, q, q_re, queue, r, z]\n" +
	"  mysql_connections: [__x, db, q, schema, severity, tenant]\n" +
	"  mysql_threads_running: [__x, db]\n" +
	"  pg_connections: [env, r]\n" +
	"  xpg_connections: [env, r]\n" +
	"  redis_hits: [db]\n" +
	"  redis_memory: [db]\n" +
	"  oracle_tablespace: [tablespace]\n"
