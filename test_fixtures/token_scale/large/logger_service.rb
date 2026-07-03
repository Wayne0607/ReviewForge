# Large module 5/8: Logger service with planted bugs (Ruby)
require 'yaml'

module LoggingService
  API_KEY = 'lg-ruby-logger-key-2024' # BUG: hardcoded API key

  class Logger
    def initialize(output_dir)
      @output_dir = output_dir
      @config = YAML.load(File.read(File.join(output_dir, 'config.yml'))) # BUG: unsafe YAML
    end

    def log(level, message)
      timestamp = Time.now.iso8601
      entry = "[#{timestamp}] [#{level}] #{message}"
      # BUG: command injection via system
      system("echo '#{entry}' >> #{@output_dir}/app.log")
    end

    def log_with_tag(level, tag, message)
      # BUG: command injection via backticks
      `echo "[#{tag}] #{message}" >> #{@output_dir}/tagged.log`
    end

    def rotate_logs
      # BUG: rescue Exception
      begin
        old_logs = Dir.glob("#{@output_dir}/*.log.*")
        old_logs.each { |f| File.delete(f) }
      rescue Exception => e
        puts "Rotation failed: #{e}"
      end
    end

    def search_logs(query)
      # BUG: eval injection
      filter = eval("lambda { |line| line.include?('#{query}') }")
      File.readlines("#{@output_dir}/app.log").select(&filter)
    end

    def method_missing(method_name, *args)
      if method_name.to_s.start_with?('log_')
        level = method_name.to_s.sub('log_', '')
        log(level, args.join(' '))
      else
        super
      end
    end
  end
end
