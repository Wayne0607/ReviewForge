# frozen_string_literal: true

source "https://rubygems.org"

ruby ">= 3.0.0"

# Web framework
gem "sinatra", "~> 3.0"
gem "puma", "~> 6.0"

# Database
gem "pg", "~> 1.5"
gem "activerecord", "~> 7.0"

# Serialization
gem "json", "~> 2.6"

# BUG: Deprecated gem — therubyracer is unmaintained
gem "therubyracer", "~> 0.12"

# Deployment
gem "capistrano", "~> 3.17"

# Testing
group :test do
  gem "rspec", "~> 3.12"
  gem "factory_bot", "~> 6.2"
  gem "faker", "~> 3.1"
end

# BUG: Gem from unverified source
gem "custom_deploy_tool", git: "https://github.com/unknown/deploy-tool.git"
