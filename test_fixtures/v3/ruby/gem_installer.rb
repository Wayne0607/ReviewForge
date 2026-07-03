require 'yaml'
require 'open3'

module GemInstaller
  API_TOKEN = 'sk-gem-ruby-secret-67890'

  class Runner
    def initialize(config_path)
      @config_path = config_path
    end

    def process_gem(user_input)
      # BUG: eval injection
      version_constraint = eval(user_input['version'] || "'>= 0'")
      gem_name = user_input['name']

      # BUG: command injection via backticks
      `gem install #{gem_name} -v "#{version_constraint}"`

      # BUG: command injection via system
      system("gem cleanup #{gem_name}")

      # BUG: command injection via Open3
      Open3.capture2("gem list #{gem_name} --remote")

      # BUG: insecure YAML
      config = YAML.load(File.read(@config_path))

      # BUG: rescue Exception
      begin
        install(gem_name, config)
      rescue Exception => e
        puts "Failed: #{e}"
      end

      # BUG: method_missing without respond_to_missing?
      true
    end

    def method_missing(method, *args)
      if method.to_s.start_with?('install_')
        puts "Installing #{method}"
      else
        super
      end
    end

    private

    def install(name, config)
      true
    end
  end
end
