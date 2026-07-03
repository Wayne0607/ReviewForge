// SQL injection variants in Go.
// Purpose: verify Go-specific SQLi patterns are detected.
package data

import (
	"database/sql"
	"fmt"
)

func QueryUserConcat(db *sql.DB, userID string) error {
	// BUG: SQL injection via string concatenation
	query := "SELECT * FROM users WHERE id = '" + userID + "'"
	_, err := db.Query(query)
	return err
}

func QueryUserSprintf(db *sql.DB, name string) error {
	// BUG: SQL injection via fmt.Sprintf
	query := fmt.Sprintf("SELECT * FROM users WHERE name = '%s'", name)
	_, err := db.Query(query)
	return err
}

func DeleteOrderSprintf(db *sql.DB, orderID string) error {
	// BUG: SQL injection in DELETE with Sprintf
	q := fmt.Sprintf("DELETE FROM orders WHERE id = '%s'", orderID)
	_, err := db.Exec(q)
	return err
}
