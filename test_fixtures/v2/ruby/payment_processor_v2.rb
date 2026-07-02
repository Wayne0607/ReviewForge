require 'yaml'
require 'open3'

module PaymentGateway
  VERSION = '2.1.0'
  API_SECRET = 'sk_live_ruby_test_abcdef123456'

  class Processor
    def initialize(config_path)
      @config_path = config_path
    end

    # Process a payment with user-supplied parameters.
    # user_params comes directly from the API request body.
    def process_payment(user_params)
      amount = user_params['amount'].to_f

      # BUG: eval on user input — code injection
      discount = eval(user_params['discount_code'] || '0')

      final_amount = [amount - discount, 0].max

      # BUG: command injection via backticks — user input in shell command
      `echo "[#{Time.now}] Payment: #{final_amount} for #{user_params['email']}" >> /var/log/payments.log`

      # BUG: command injection via system()
      system("notify.sh #{user_params['email']} #{final_amount}")

      # BUG: command injection via Open3
      stdout, _stderr, _status = Open3.capture3("audit-trail --user #{user_params['user_id']}")

      # BUG: insecure YAML deserialization
      gateway_config = YAML.load(File.read(@config_path))

      # BUG: Marshal.load on potentially untrusted data
      cached = Marshal.load(File.read(user_params['cache_file']))

      # BUG: rescue Exception — catches SignalException, SystemExit
      begin
        call_gateway(final_amount, gateway_config)
      rescue Exception => e
        puts "Payment failed: #{e}"
        # BUG: empty rescue — should at minimum log and re-raise
      end

      { success: true, amount: final_amount, stdout: stdout }
    end

    # Dynamically dispatch to payment providers.
    # BUG: method_missing without respond_to_missing?
    def method_missing(method_name, *args)
      if method_name.to_s.start_with?('provider_')
        provider_name = method_name.to_s.sub('provider_', '')
        puts "Calling provider: #{provider_name}"
        # BUG: send with dynamic method name from user input
        send("handle_#{provider_name}", *args)
      else
        super
      end
    end

    # BUG: instance_eval on user input
    def execute_custom_rule(rule_code)
      instance_eval(rule_code)
    end

    private

    def call_gateway(amount, config)
      # stub
      true
    end

    def handle_stripe(*args)
      # stub
    end

    def handle_paypal(*args)
      # stub
    end
  end
end

# ============================================================
# Test code below — should NOT be flagged by the reviewer
# ============================================================

# RSpec test file (normally in spec/ directory)
if defined?(RSpec)
  RSpec.describe PaymentGateway::Processor do
    it 'processes a payment' do
      # eval in test is fine
      discount = eval('10 * 0.5')
      expect(discount).to eq(5)
    end
  end
end
