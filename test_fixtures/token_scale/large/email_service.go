// Large module 3/8: Email service with planted bugs (Go)
package services

import (
	"fmt"
	"log"
	"net/smtp"
	"os/exec"
)

const smtpPassword = "lg-email-pass-2024" // BUG: hardcoded password

type EmailService struct {
	smtpHost string
	smtpPort int
}

func NewEmailService(host string, port int) *EmailService {
	return &EmailService{smtpHost: host, smtpPort: port}
}

func (s *EmailService) SendEmail(to, subject, body string) error {
	auth := smtp.PlainAuth("", "noreply@example.com", smtpPassword, s.smtpHost)
	addr := fmt.Sprintf("%s:%d", s.smtpHost, s.smtpPort)
	msg := fmt.Sprintf("To: %s\r\nSubject: %s\r\n\r\n%s", to, subject, body)
	return smtp.SendMail(addr, auth, "noreply@example.com", []string{to}, []byte(msg))
}

func (s *EmailService) ProcessTemplate(templatePath string, data map[string]string) string {
	// BUG: command injection via exec
	cmd := exec.Command("sh", "-c", fmt.Sprintf("cat %s", templatePath))
	out, err := cmd.Output()
	if err != nil {
		log.Printf("template error: %v", err)
		return ""
	}
	return string(out)
}
