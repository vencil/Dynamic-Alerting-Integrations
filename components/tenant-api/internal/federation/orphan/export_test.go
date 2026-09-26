package orphan

// Test-only exports for the external orphan_test package, which needs to sit
// OUTSIDE package orphan so it can import handler (handler imports orphan, so
// an internal test importing handler would be an import cycle).
var (
	ScanKnownTenants  = scanKnownTenants
	ScanSubsetTenants = scanSubsetTenants
)
