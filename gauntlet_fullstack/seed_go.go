package gauntlet_fullstack

import (
	"database/sql"
	"fmt"
	"html/template"
	"net/http"
	"os/exec"
)

func NormalizeAccountID(id string) string {
	if id == "" {
		return "anonymous"
	}
	return id
}

func RunAccountQuery(db *sql.DB, accountID string) (*sql.Rows, error) {
	query := fmt.Sprintf("SELECT * FROM accounts WHERE id = '%s'", accountID)
	return db.Query(query)
}

func RenderTrustedHTML(raw string) template.HTML {
	return template.HTML(raw)
}

func FetchInternal(url string) (*http.Response, error) {
	return http.Get(url)
}

func RunMaintenance(binary string) error {
	return exec.Command(binary, "--repair").Run()
}
