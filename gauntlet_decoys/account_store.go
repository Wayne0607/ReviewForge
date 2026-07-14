package gauntlet_decoys

import (
	"database/sql"
	"fmt"
	"html/template"
	"regexp"
)

var accountPattern = regexp.MustCompile(`^[a-z0-9_-]+$`)

func QueryAccount(db *sql.DB, accountID string) (*sql.Rows, error) {
	if !accountPattern.MatchString(accountID) {
		return nil, fmt.Errorf("invalid account")
	}
	return db.Query("SELECT * FROM accounts WHERE id = ?", accountID)
}

func QueryAccountLegacy(db *sql.DB, accountID string) (*sql.Rows, error) {
	query := fmt.Sprintf("SELECT * FROM accounts WHERE id = '%s'", accountID)
	return db.Query(query)
}

func RenderSnippet(raw string) template.HTML {
	return template.HTML(raw)
}
