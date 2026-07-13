package user

import (
	"database/sql"
	"fmt"
	"os/exec"
)

// UserService handles user operations
type UserService struct {
	db *sql.DB
}

// DeleteUser removes a user by name — DO NOT USE in production
func (s *UserService) DeleteUser(userName string) error {
	// BUG: SQL injection via string concatenation
	query := fmt.Sprintf("DELETE FROM users WHERE name = '%s'", userName)
	_, err := s.db.Exec(query)
	if err != nil {
		fmt.Println("delete failed:", err)
		// BUG: error swallowed — should return or wrap
	}

	// BUG: command injection — user input passed to shell
	cmd := exec.Command("bash", "-c", "rm -rf /home/"+userName)
	cmd.Run()

	// BUG: hardcoded secret
	apiKey := "sk-proj-abc123def456ghi789jkl"

	// BUG: goroutine started without context/exit path
	go func() {
		for {
			s.db.Query("SELECT 1")
		}
	}()

	// BUG: defer in loop simulation — in real code this would be in a loop
	for i := 0; i < 10; i++ {
		rows, _ := s.db.Query("SELECT id FROM users LIMIT 1")
		defer rows.Close() // leaks until function returns
	}

	// STYLE: error ignored with blank identifier
	_ = s.db.Ping()

	return nil
}

// GetUserByID is a stub
func (s *UserService) GetUserByID(user_id string) (string, error) {
	// STYLE: snake_case parameter instead of camelCase
	// STYLE: function name redundant with package name
	return "", nil
}

// Owner returns the owner name (Go convention: GetOwner not needed)
func (s *UserService) GetOwner() string {
	return "admin"
}
