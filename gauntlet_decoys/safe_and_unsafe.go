package gauntlet_decoys

import (
	"database/sql"
	"fmt"
	"html/template"
	"regexp"
)

var accountPattern = regexp.MustCompile(`^[a-z0-9_-]+$`)

func SafeQuery(db *sql.DB, accountID string) (*sql.Rows, error) {
	if !accountPattern.MatchString(accountID) {
		return nil, fmt.Errorf("invalid account")
	}
	return db.Query("SELECT * FROM accounts WHERE id = ?", accountID)
}

func UnsafeQuery(db *sql.DB, accountID string) (*sql.Rows, error) {
	query := fmt.Sprintf("SELECT * FROM accounts WHERE id = '%s'", accountID)
	return db.Query(query)
}

func UnsafeHTML(raw string) template.HTML {
	return template.HTML(raw)
}
