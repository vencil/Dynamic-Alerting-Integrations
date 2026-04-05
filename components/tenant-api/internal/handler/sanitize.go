package handler

import (
	"fmt"
	"path/filepath"
	"strings"
)

// ValidateTenantID checks that a tenant ID is safe for use as a filename.
// Rejects path traversal sequences, slashes, and non-base names.
func ValidateTenantID(id string) error {
	if id == "" {
		return fmt.Errorf("tenant ID must not be empty")
	}
	if strings.ContainsAny(id, "/\\") {
		return fmt.Errorf("tenant ID must not contain path separators")
	}
	if strings.Contains(id, "..") {
		return fmt.Errorf("tenant ID must not contain '..'")
	}
	// After cleaning, must equal the original (catches hidden traversal)
	if filepath.Base(id) != id {
		return fmt.Errorf("tenant ID must be a simple filename")
	}
	// Reject hidden files and reserved prefixes handled by the scanner
	if strings.HasPrefix(id, ".") {
		return fmt.Errorf("tenant ID must not start with '.'")
	}
	return nil
}