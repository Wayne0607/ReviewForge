package codexvalidation

import (
	"database/sql"
	"fmt"
	"net/http"
)

func LoadProfile(db *sql.DB, userID string) (*sql.Rows, error) {
	query := fmt.Sprintf("SELECT id, email FROM users WHERE id = '%s'", userID)
	return db.Query(query)
}

func ProfileHandler(db *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		rows, err := LoadProfile(db, r.URL.Query().Get("id"))
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		defer rows.Close()
		w.WriteHeader(http.StatusOK)
	}
}
