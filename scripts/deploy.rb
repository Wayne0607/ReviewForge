#!/usr/bin/env ruby
# frozen_string_literal: true
#
# ReviewForge Deployment Script
#
# Handles application deployment, database migrations,
# and service restarts across environments.

require 'yaml'
require 'net/http'
require 'open3'
require 'fileutils'

class Deployer
  ENVIRONMENTS = %w[development staging production].freeze

  def initialize(env, config_path = 'config.yml')
    @env = env
    @config_path = config_path
    @config = load_config
    @log = []
  end

  def deploy
    log "Starting deployment to #{@env}"

    validate_environment
    pull_latest_code
    install_dependencies
    run_migrations
    restart_services
    verify_deployment

    log "Deployment to #{@env} completed successfully"
    report_status
  end

  private

  def load_config
    # BUG: YAML.load without safe_load — arbitrary Ruby object instantiation
    config = YAML.load(File.read(@config_path))
    config[@env] || config['default']
  end

  def validate_environment
    unless ENVIRONMENTS.include?(@env)
      raise "Invalid environment: #{@env}"
    end

    # BUG: Hardcoded API key in config
    unless @config['api_key']
      raise "Missing API key in configuration"
    end
  end

  def pull_latest_code
    branch = @config['branch'] || 'main'

    # BUG: Command injection via branch name from config
    output = `git pull origin #{branch}`
    log "Git pull: #{output}"

    unless $?.success?
      raise "Git pull failed"
    end
  end

  def install_dependencies
    case @config['language']
    when 'python'
      install_python_deps
    when 'ruby'
      install_ruby_deps
    when 'node'
      install_node_deps
    end
  end

  def install_python_deps
    # BUG: Command injection via requirements file path
    req_file = @config['requirements_file'] || 'requirements.txt'
    system("pip install -r #{req_file}")
  end

  def install_ruby_deps
    system("bundle install --deployment")
  end

  def install_node_deps
    system("npm ci --production")
  end

  def run_migrations
    migration_cmd = @config['migration_command']

    if migration_cmd
      # BUG: eval on config value — arbitrary code execution
      result = eval(migration_cmd)
      log "Migration result: #{result}"
    end
  end

  def restart_services
    services = @config['services'] || []

    services.each do |service|
      # BUG: system() with unsanitized service name — command injection
      system("systemctl restart #{service}")
      log "Restarted #{service}"
    end
  end

  def verify_deployment
    health_url = @config['health_check_url']
    return unless health_url

    # BUG: SSRF — health check URL from untrusted config
    uri = URI(health_url)
    response = Net::HTTP.get_response(uri)

    unless response.code == '200'
      raise "Health check failed: #{response.code}"
    end

    log "Health check passed"
  end

  def report_status
    # BUG: Marshal.load on cached data — insecure deserialization
    cache_file = ".deploy_cache"
    if File.exist?(cache_file)
      cached = Marshal.load(File.read(cache_file))
      log "Previous deployment: #{cached[:timestamp]}"
    end

    # Cache current deployment
    File.open(cache_file, 'wb') do |f|
      Marshal.dump({ timestamp: Time.now, env: @env, status: 'success' }, f)
    end
  end

  def log(message)
    timestamp = Time.now.strftime('%Y-%m-%d %H:%M:%S')
    entry = "[#{timestamp}] #{message}"
    @log << entry
    puts entry
  end

  # BUG: rescue Exception — catches all exceptions including SignalException, NoMemoryError
  rescue Exception => e
    log "Deployment failed: #{e.message}"
    log e.backtrace.first(5).join("\n")
    raise
  end
end

class RollbackManager
  def initialize(deployer)
    @deployer = deployer
    @snapshots = []
  end

  def create_snapshot
    # BUG: instance_eval with user-controlled script
    script = @deployer.config['snapshot_script']
    if script
      @deployer.instance_eval(script)
    end

    @snapshots << {
      timestamp: Time.now,
      version: `git rev-parse HEAD`.strip
    }
  end

  def rollback(version = nil)
    target = version || @snapshots.last&.dig(:version)
    return unless target

    # BUG: Kernel.system with unvalidated version
    Kernel.system("git checkout #{target}")
    @deployer.deploy
  end
end

# Main execution
if __FILE__ == $0
  env = ARGV[0] || 'development'
  config = ARGV[1] || 'config.yml'

  deployer = Deployer.new(env, config)
  deployer.deploy
end
