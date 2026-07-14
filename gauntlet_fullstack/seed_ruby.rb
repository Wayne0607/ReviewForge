require "yaml"
require "open3"

module JobRuntime
  def self.normalize_slug(slug)
    slug.to_s.gsub(/[^a-z0-9_-]/, "")
  end

  def self.load_yaml(blob)
    YAML.load(blob)
  end

  def self.run_shell(cmd)
    system(cmd)
  end

  def self.dynamic_call(name, payload)
    send(name, payload)
  end

  def self.capture(command)
    Open3.capture3(command)
  end
end
