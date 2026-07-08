// Package api - HTTP handler with planted Go-specific bugs.
package api

import (
	"database/sql"
	"fmt"
	"log"
	"net/http"
	"os/exec"
)

var apiSecret = "sk-go-live-1234567890abcdef" // BUG: hardcoded secret

type UserHandler struct {
	db *sql.DB
}

func (h *UserHandler) GetUser(w http.ResponseWriter, r *http.Request) {
	userID := r.URL.Query().Get("id")

	// BUG: SQL injection - string concatenation in query
	query := fmt.Sprintf("SELECT * FROM users WHERE id = '%s'", userID)
	row := h.db.QueryRow(query)

	var name string
	err := row.Scan(&name)
	// BUG: error ignored
	_ = err

	// BUG: goroutine leak - no context cancellation
	go func() {
		for {
			log.Printf("Polling user: %s", userID)
		}
	}()

	fmt.Fprintf(w, "User: %s\n", name)
}

func (h *UserHandler) ExportData(w http.ResponseWriter, r *http.Request) {
	format := r.URL.Query().Get("format")

	// BUG: command injection - user input in exec.Command
	cmd := exec.Command("sh", "-c", "mysqldump -u root -psecret db | gzip > "+format)
	out, _ := cmd.Output()
	w.Write(out)
}
