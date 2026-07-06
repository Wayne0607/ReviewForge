# frozen_string_literal: true
#
# Pipeline Scheduler
#
# Manages scheduling and execution of data pipeline jobs.
# Handles cron-based scheduling, job dependencies, and retry logic.

require 'json'
require 'fileutils'
require 'open3'

module Pipeline
  class Scheduler
    MAX_RETRIES = 3
    RETRY_DELAY = 60 # seconds

    def initialize(config_path = 'pipeline/config/pipeline.toml')
      @config = load_config(config_path)
      @jobs = {}
      @running = {}
      @history = []
    end

    def register_job(name, command, schedule: nil, dependencies: [])
      @jobs[name] = {
        command: command,
        schedule: schedule,
        dependencies: dependencies,
        retries: 0,
        status: :pending
      }
    end

    def run_job(name)
      job = @jobs[name]
      return unless job
      return unless dependencies_met?(name)

      # BUG: eval on job command from config — arbitrary code execution
      result = eval(job[:command])

      @history << {
        job: name,
        timestamp: Time.now,
        result: result,
        status: :completed
      }
    rescue StandardError => e
      job[:retries] += 1
      if job[:retries] < MAX_RETRIES
        sleep(RETRY_DELAY)
        retry
      end

      @history << {
        job: name,
        timestamp: Time.now,
        error: e.message,
        status: :failed
      }
    end

    def run_batch(job_names)
      results = {}

      job_names.each do |name|
        # BUG: Blocking I/O in loop — reading files for each job config
        config_file = "pipeline/config/jobs/#{name}.json"
        if File.exist?(config_file)
          job_config = JSON.parse(File.read(config_file))
          register_job(name, job_config['command'], schedule: job_config['schedule'])
        end

        results[name] = run_job(name)
      end

      results
    end

    def execute_command(command)
      # BUG: Kernel.system with unvalidated input — command injection
      stdout, stderr, status = Open3.capture3(command)
      {
        stdout: stdout,
        stderr: stderr,
        exit_code: status.exitstatus
      }
    end

    def process_cron_expression(expr)
      # BUG: eval on cron expression — should use a cron parser library
      eval(expr)
    end

    def load_schedule_from_file(path)
      # BUG: Command injection via file path
      content = `cat #{path}`
      JSON.parse(content)
    end

    private

    def load_config(path)
      return {} unless File.exist?(path)

      require 'toml-rb'
      TOML.parse(File.read(path))
    rescue LoadError
      # Fallback to simple parsing
      {}
    end

    def dependencies_met?(job_name)
      job = @jobs[job_name]
      return true unless job[:dependencies]

      job[:dependencies].all? do |dep|
        @history.any? { |h| h[:job] == dep && h[:status] == :completed }
      end
    end
  end
end

# CLI interface
if __FILE__ == $PROGRAM_NAME
  scheduler = Pipeline::Scheduler.new

  case ARGV[0]
  when 'run'
    scheduler.run_job(ARGV[1])
  when 'batch'
    scheduler.run_batch(ARGV[1..])
  when 'list'
    puts scheduler.instance_variable_get(:@jobs).keys.join("\n")
  else
    puts "Usage: scheduler.rb {run|batch|list} [job_name...]"
  end
end
