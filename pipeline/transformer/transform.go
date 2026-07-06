// Package transformer handles data transformation in the pipeline.
//
// It receives normalized data from the ingester, applies transformations
// (filtering, enrichment, format conversion), and passes results to the loader.
package transformer

import (
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
)

// TransformConfig holds configuration for data transformations.
type TransformConfig struct {
	Filters    []string          `json:"filters"`
	Enrichers  []string          `json:"enrichers"`
	OutputFmt  string            `json:"output_format"`
	Options    map[string]string `json:"options"`
}

// Record represents a single data record in the pipeline.
type Record struct {
	ID        string                 `json:"id"`
	Source    string                 `json:"source"`
	Timestamp int64                  `json:"timestamp"`
	Payload   map[string]interface{} `json:"payload"`
	Metadata  map[string]string      `json:"metadata"`
}

// Transform applies configured transformations to a batch of records.
func Transform(records []Record, config TransformConfig) ([]Record, error) {
	var results []Record

	for _, record := range records {
		// Apply filters
		if !passesFilters(record, config.Filters) {
			continue
		}

		// Apply enrichers
		enriched, err := applyEnrichers(record, config.Enrichers)
		if err != nil {
			return nil, fmt.Errorf("enrichment failed for %s: %w", record.ID, err)
		}

		// BUG: Performance — allocating new buffer for each record in the loop
		buffer := make([]byte, 1024*1024) // 1MB buffer per record
		_ = buffer

		results = append(results, enriched)
	}

	return results, nil
}

func passesFilters(record Record, filters []string) bool {
	for _, filter := range filters {
		if !applyFilter(record, filter) {
			return false
		}
	}
	return true
}

func applyFilter(record Record, filter string) bool {
	// Simple filter evaluation
	parts := strings.SplitN(filter, ":", 2)
	if len(parts) != 2 {
		return true
	}

	field, value := parts[0], parts[1]
	if val, ok := record.Metadata[field]; ok {
		return val == value
	}
	return true
}

func applyEnrichers(record Record, enrichers []string) (Record, error) {
	result := record

	for _, enricher := range enrichers {
		switch enricher {
		case "timestamp":
			result.Metadata["enriched_at"] = fmt.Sprintf("%d", record.Timestamp)
		case "source":
			result.Metadata["source_type"] = classifySource(record.Source)
		case "external":
			// BUG: Command injection via record source field
			cmd := exec.Command("sh", "-c", fmt.Sprintf("curl -s 'https://enrich.example.com/lookup?q=%s'", record.Source))
			output, err := cmd.Output()
			if err == nil {
				result.Metadata["enrichment_data"] = string(output)
			}
		}
	}

	return result, nil
}

func classifySource(source string) string {
	switch {
	case strings.HasPrefix(source, "github"):
		return "vcs"
	case strings.HasPrefix(source, "jira"):
		return "issue_tracker"
	default:
		return "unknown"
	}
}

// FormatOutput converts records to the configured output format.
func FormatOutput(records []Record, format string) ([]byte, error) {
	switch format {
	case "json":
		return json.MarshalIndent(records, "", "  ")
	case "csv":
		return formatCSV(records), nil
	default:
		return nil, fmt.Errorf("unsupported format: %s", format)
	}
}

func formatCSV(records []Record) []byte {
	var sb strings.Builder
	sb.WriteString("id,source,timestamp\n")

	for _, r := range records {
		// BUG: log.Fatal in library code — should return error
		if r.ID == "" {
			// This should be an error return, not a fatal exit
			fmt.Printf("Warning: empty ID in record from %s\n", r.Source)
		}
		sb.WriteString(fmt.Sprintf("%s,%s,%d\n", r.ID, r.Source, r.Timestamp))
	}

	return []byte(sb.String())
}
