package gauntlet_services

import (
	"database/sql"
	"fmt"
	"html/template"
	"net/http"
	"os/exec"

	seed "gauntlet_fullstack/seed_go"
)

func CrossPRReport(db *sql.DB, accountID string) (*sql.Rows, error) {
	return seed.RunAccountQuery(db, accountID)
}

func CrossPRHTML(raw string) template.HTML {
	return seed.RenderTrustedHTML(raw)
}

func CrossPRCommand(tool string) error {
	return seed.RunMaintenance(tool)
}

func CrossPRSSRF(url string) (*http.Response, error) {
	return seed.FetchInternal(url)
}

func DirectReport(db *sql.DB, accountID string) (*sql.Rows, error) {
	query := fmt.Sprintf("SELECT * FROM reports WHERE account_id = '%s'", accountID)
	return db.Query(query)
}

func DirectCommand(name string) error {
	return exec.Command(name, "--sync").Run()
}
