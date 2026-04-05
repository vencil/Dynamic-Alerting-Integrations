package handler

import "testing"

func TestValidateTenantID(t *testing.T) {
	tests := []struct {
		id      string
		wantErr bool
	}{
		// Valid
		{"db-a", false},
		{"prod-mariadb-01", false},
		{"tenant_123", false},

		// Empty
		{"", true},

		// Path traversal
		{"../etc/passwd", true},
		{"..%2f..%2fetc", true}, // %2f decoded by chi before reaching handler
		{"foo/../bar", true},
		{"..", true},

		// Slashes
		{"foo/bar", true},
		{"foo\\bar", true},

		// Hidden files
		{".hidden", true},
		{".git", true},

		// filepath.Base mismatch
		{".", true},
	}

	for _, tt := range tests {
		t.Run(tt.id, func(t *testing.T) {
			err := ValidateTenantID(tt.id)
			if (err != nil) != tt.wantErr {
				t.Errorf("ValidateTenantID(%q) error = %v, wantErr %v", tt.id, err, tt.wantErr)
			}
		})
	}
}
