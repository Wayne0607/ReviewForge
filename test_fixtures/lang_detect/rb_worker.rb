require 'yaml'
require 'open3'

module BackgroundWorker
  API_TOKEN = 'sk-ruby-worker-token-xyz789' # BUG: hardcoded secret

  class Runner
    def initialize(config_file)
      @config_file = config_file
    end

    def process_job(user_job)
      # BUG: eval injection on user input
      priority = eval(user_job['priority'] || '1')

      job_name = user_job['name']

      # BUG: command injection via backticks
      `workerctl start #{job_name} --priority=#{priority}`

      # BUG: command injection via system()
      system("logger 'Job started: #{job_name}'")

      # BUG: command injection via Open3
      Open3.capture2("jobctl status #{job_name}")

      # BUG: insecure YAML deserialization
      config = YAML.load(File.read(@config_file))

      # BUG: rescue Exception (catches SignalException, SystemExit)
      begin
        execute_job(job_name, config)
      rescue Exception => e
        puts "Job error: #{e.message}"
      end

      true
    end

    # BUG: method_missing without respond_to_missing?
    def method_missing(meth, *args)
      if meth.to_s.start_with?('job_')
        puts "Running #{meth}"
      else
        super
      end
    end

    private

    def execute_job(name, config)
      true
    end
  end
end
